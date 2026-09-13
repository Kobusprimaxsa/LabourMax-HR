"""The employee — phase P4, domain 5.

``employee`` holds **identity only**. Everything that changes over time — pay,
position, bank account, schedule, tax profile — lives in its own effective-dated
child table, because invariant 2 says a March 2026 payroll must calculate
identically when re-run in 2029, and that is impossible if the rate was overwritten
in July.

Three things on this table are worth reading before changing anything.

**The ID number is encrypted, and the hash beside it is scoped to the tenant.**
``id_number_hash`` answers one question — "have we captured this person already" —
without decrypting anything. Scoping it matters and is easy to miss: an unscoped
HMAC produces the *same* digest for the same person in every tenant, so anyone
holding the column can tell that an employee of one employer is an employee of
another. Nothing decrypts and no policy is bypassed, and a fact has still crossed
the tenant boundary — a leak the isolation suite cannot see, because no forbidden
row was ever read (D-95).

**The two sort columns are generated and carry an ICU collation, set here in the
creating migration.** Changing a collation afterwards rewrites the table and every
index on it (D-17), so this is one of the few decisions that is genuinely cheaper
to get right now than later. Deterministic ICU, and lower-cased in the expression
rather than by a case-insensitive collation, so ``LIKE`` and index-backed search
keep working.

**The two cache columns are denormalised on purpose** (D-18). Pay basis lives on an
effective-dated table, so grouping an employee list by it would join and date-filter
on every render. They are maintained on write **and** by a nightly job, because a
future-dated increase has to flip on its own effective date with nobody touching the
record. The job arrives with the remuneration table; until then these stay NULL,
which is honest — NULL means nobody has computed it, not "no pay group".
"""

from __future__ import annotations

import datetime
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Now

from core.audit import AuditedModel
from core.db.fields import EncryptedCharField, keyed_hash, last4
from core.models import TenantScopedModel
from employees import identity
from employers.models import PROVINCE_CODES


class Employee(AuditedModel, TenantScopedModel):
    """A person employed by an employer. Identity and demographics only."""

    class IdType(models.TextChoices):
        SA_ID = "sa_id", "South African ID"
        PASSPORT = "passport", "Passport"
        ASYLUM_PERMIT = "asylum_permit", "Asylum seeker permit"
        WORK_PERMIT = "work_permit", "Work permit"

    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        OTHER = "other", "Other"
        NOT_SPECIFIED = "not_specified", "Not specified"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ON_LEAVE = "on_leave", "On leave"
        SUSPENDED = "suspended", "Suspended"
        TERMINATED = "terminated", "Terminated"
        ARCHIVED = "archived", "Archived"

    #: Statuses that count toward the subscription headcount, per D-33: active
    #: means not terminated and not archived.
    BILLABLE_STATUSES = {Status.DRAFT, Status.ACTIVE, Status.ON_LEAVE, Status.SUSPENDED}

    #: Only a South African ID number carries a checksum this system can verify.
    #: A passport or a permit has none, so ``id_number_verified`` stays false and
    #: means what it says: nobody has verified this.
    VERIFIABLE_ID_TYPES = {IdType.SA_ID}

    employer = models.ForeignKey(
        "employers.Employer", on_delete=models.PROTECT, related_name="employees"
    )
    public_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    employee_number = models.CharField(
        max_length=30,
        blank=True,
        db_index=True,
        help_text="Unique within the employer. Generated when blank, and editable after.",
    )

    first_name = models.CharField(max_length=80, db_index=True)
    middle_names = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=80, db_index=True)
    preferred_name = models.CharField(max_length=80, blank=True)
    initials = models.CharField(max_length=10, blank=True, help_text="Required on the IRP5.")

    date_of_birth = models.DateField(
        help_text="Drives age-based rebates and the BCEA s43 minimum-age block."
    )
    gender = models.CharField(
        max_length=20,
        choices=Gender.choices,
        blank=True,
        help_text="Collected for the EEA return and the UI-19 only.",
    )
    nationality_code = models.CharField(max_length=2, default="ZA")
    home_language = models.CharField(
        max_length=40, blank=True, help_text="Selects the contract language."
    )

    id_type = models.CharField(max_length=20, choices=IdType.choices, default=IdType.SA_ID)
    id_number = EncryptedCharField(
        max_plaintext_length=30,
        help_text="Encrypted at rest. POPIA special personal information; never queryable.",
    )
    id_number_last4 = models.CharField(max_length=4, blank=True, editable=False)
    id_number_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        db_index=True,
        help_text="Keyed HMAC, scoped to the tenant. The duplicate-employee guard.",
    )
    id_number_verified = models.BooleanField(
        default=False,
        editable=False,
        help_text="The checksum and the date of birth agree. Only ever true for an SA ID.",
    )

    mobile_number = models.CharField(
        max_length=20,
        db_index=True,
        help_text="Mandatory. Carries the OTP and the payslip notice where email fails.",
    )
    email = models.EmailField(
        blank=True, help_text="Mandatory unless has_no_email is set. Stored lower-cased."
    )
    has_no_email = models.BooleanField(
        default=False,
        help_text=(
            "An honest flag for a worker with no address. Routes delivery to SMS and "
            "stops employers inventing addresses to satisfy a mandatory field (D-10)."
        ),
    )

    photo_file = models.ForeignKey(
        "core.FileObject", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    is_billable = models.BooleanField(
        default=True, help_text="Counts toward the subscription headcount."
    )

    first_engagement_date = models.DateField(null=True, blank=True, editable=False)
    latest_termination_date = models.DateField(null=True, blank=True, editable=False)

    current_pay_group = models.ForeignKey(
        "employers.PayGroup",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
        help_text="CACHE (D-18). NULL means not yet computed, not 'no pay group'.",
    )
    current_pay_basis = models.CharField(
        max_length=20, blank=True, db_index=True, editable=False, help_text="CACHE (D-18)."
    )

    # Generated and stored, with the collation fixed here in the creating migration.
    # Lower-cased in the EXPRESSION rather than by a case-insensitive collation, so
    # the collation stays deterministic and LIKE keeps using the index (D-17).
    sort_name_first = models.GeneratedField(
        expression=models.functions.Lower(
            models.functions.Concat("first_name", models.Value(" "), "last_name")
        ),
        output_field=models.CharField(max_length=161, db_collation="en-ZA-x-icu"),
        db_persist=True,
    )
    sort_name_last = models.GeneratedField(
        expression=models.functions.Lower(
            models.functions.Concat("last_name", models.Value(" "), "first_name")
        ),
        output_field=models.CharField(max_length=161, db_collation="en-ZA-x-icu"),
        db_persist=True,
    )

    audit_sensitive_fields = ("id_number", "id_number_last4", "id_number_hash")

    class Meta:
        db_table = "employee"
        ordering = ["sort_name_last"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "sort_name_first"]),
            models.Index(fields=["tenant", "sort_name_last"]),
            models.Index(fields=["tenant", "current_pay_group"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employer", "employee_number"], name="uniq_employee_number_per_employer"
            ),
            # The duplicate-person guard. Scoped to the tenant rather than global,
            # because two different subscribers may each legitimately employ the
            # same person — a domestic worker with two households is the ordinary
            # case in this market, not an edge one.
            models.UniqueConstraint(
                fields=["tenant", "id_number_hash"], name="uniq_employee_id_number_per_tenant"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    id_type__in=["sa_id", "passport", "asylum_permit", "work_permit"]
                ),
                name="employee_id_type_is_known",
            ),
            # Stable rather than immutable, and that is safe in this direction only:
            # `< today` can only ever become MORE permissive as time passes, so a
            # row valid when written stays valid through a dump, a restore and a
            # table rewrite. The reverse comparison would not be.
            models.CheckConstraint(
                condition=models.Q(date_of_birth__lt=Now()),
                name="employee_born_before_today",
            ),
            models.CheckConstraint(
                condition=models.Q(has_no_email=True) | ~models.Q(email=""),
                name="employee_has_an_email_or_says_it_has_none",
            ),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    def age_on(self, on_date: datetime.date) -> int:
        """Completed years on a given date. Takes the date; never reads the clock."""
        return identity.age_on(self.date_of_birth, on_date)

    # ------------------------------------------------------------------ saving

    def save(self, *args, **kwargs):
        """Maintain the three companion columns and the employee number.

        Here rather than in a service function because every writer must get it,
        including a data migration, a bulk import and the shell. A hash somebody
        forgot to set silently defeats the duplicate check it exists for — the check
        then reports that every person is new, which is the failure it was built to
        prevent.
        """
        self.email = (self.email or "").strip().lower()

        if self.id_number:
            self.id_number_last4 = last4(self.id_number)
            self.id_number_hash = keyed_hash(self.id_number, scope=f"tenant:{self.tenant_id}")
            self.id_number_verified = self._id_number_checks_out()
        else:
            self.id_number_last4 = ""
            self.id_number_hash = ""
            self.id_number_verified = False

        if not self.employee_number:
            self.employee_number = next_employee_number(self.employer_id)

        if kwargs.get("update_fields") is not None:
            update_fields = set(kwargs["update_fields"])
            if "id_number" in update_fields:
                update_fields |= {"id_number_last4", "id_number_hash", "id_number_verified"}
            if "email" in update_fields:
                update_fields.add("email")
            kwargs["update_fields"] = sorted(update_fields)

        super().save(*args, **kwargs)

    def _id_number_checks_out(self) -> bool:
        """Both the checksum and the date of birth, or false.

        Requiring both is the point. The checksum catches a mistyped digit inside the
        number. The date cross-check catches a number that is internally perfect and
        belongs to somebody else, or a date of birth typed from the wrong line of the
        document — neither of which any single-field validation can see.
        """
        if self.id_type not in self.VERIFIABLE_ID_TYPES:
            return False
        if not check_is_possible(self.date_of_birth):
            return False
        return identity.check_sa_id(self.id_number).is_valid and identity.birth_date_agrees(
            self.id_number, self.date_of_birth
        )

    def clean(self):
        """Refuse a capture that contradicts itself, with the reason.

        Deliberately does NOT refuse an unverifiable number. A passport has no
        checksum, and an employer holding a passport must still be able to run
        payroll for that person — ``id_number_verified`` stays false and the screen
        can say so.
        """
        super().clean()

        if self.has_no_email and self.email:
            raise ValidationError(
                {
                    "has_no_email": (
                        "This says the employee has no email address, but an address "
                        "is captured. Clear one or the other."
                    )
                }
            )
        if not self.has_no_email and not self.email:
            raise ValidationError(
                {
                    "email": (
                        "An email address is required. If this employee genuinely has "
                        "none, tick 'no email address' — payslips then go by SMS, and "
                        "nobody has to invent an address to get past this field."
                    )
                }
            )

        if self.id_type != self.IdType.SA_ID or not self.id_number:
            return

        check = identity.check_sa_id(self.id_number)
        if check.failed:
            raise ValidationError({"id_number": " ".join(check.reasons)})

        if self.date_of_birth and not identity.birth_date_agrees(
            self.id_number, self.date_of_birth
        ):
            readable = ", ".join(
                candidate.strftime("%d %B %Y") for candidate in check.birth_date_candidates
            )
            raise ValidationError(
                {
                    "date_of_birth": (
                        f"The ID number says this person was born on {readable}, but the "
                        f"date of birth captured is "
                        f"{self.date_of_birth.strftime('%d %B %Y')}. One of the two is "
                        f"wrong, and the ID number is the one that will reach SARS."
                    )
                }
            )


def check_is_possible(date_of_birth) -> bool:
    return isinstance(date_of_birth, datetime.date)


def next_employee_number(employer_id: int) -> str:
    """The next number for an employer, as ``EMP0001``.

    A query rather than a sequence, because the number is user-editable: an employer
    migrating from a paper system types in the numbers they already use, and a
    database sequence would drift away from them immediately and never come back.

    Two concurrent captures can compute the same number. The unique constraint on
    ``(employer, employee_number)`` is what makes that a visible error rather than a
    silent duplicate, and it is the right place for the guarantee — a lock here
    would serialise every capture for the whole of an employer's onboarding.
    """
    existing = (
        Employee.objects.filter(employer_id=employer_id, employee_number__startswith="EMP")
        .values_list("employee_number", flat=True)
        .order_by()
    )
    highest = 0
    for number in existing:
        suffix = number[3:]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"EMP{highest + 1:04d}"


class EmployeeAddress(AuditedModel, TenantScopedModel):
    """Residential and postal address history.

    Effective-dated rather than overwritten: a UI-19 or an IRP5 reissued for a past
    year must carry the address as it stood then, not the one the employee moved to
    afterwards.
    """

    class AddressType(models.TextChoices):
        RESIDENTIAL = "residential", "Residential"
        POSTAL = "postal", "Postal"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="addresses")
    address_type = models.CharField(
        max_length=20, choices=AddressType.choices, default=AddressType.RESIDENTIAL
    )

    line1 = models.CharField(max_length=150)
    line2 = models.CharField(max_length=150, blank=True)
    suburb = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100)
    province_code = models.CharField(max_length=10, choices=PROVINCE_CODES, blank=True)
    postal_code = models.CharField(max_length=10, blank=True)
    country_code = models.CharField(max_length=2, default="ZA")

    effective_from = models.DateField()
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means current."
    )

    class Meta:
        db_table = "employee_address"
        ordering = ["employee_id", "address_type", "-effective_from"]
        indexes = [
            models.Index(fields=["employee", "address_type", "-effective_from"]),
            models.Index(fields=["tenant"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="employee_address_period_ordered",
            ),
        ]

    def __str__(self):
        return f"{self.address_type} address for {self.employee_id}"


class EmployeeContact(AuditedModel, TenantScopedModel):
    """Next of kin and emergency contacts.

    Not effective-dated, and that is the workbook's call rather than an oversight:
    nothing statutory is reported from this table, so there is no past state anyone
    has to reproduce. It is the number somebody rings.
    """

    class ContactType(models.TextChoices):
        EMERGENCY = "emergency", "Emergency contact"
        NEXT_OF_KIN = "next_of_kin", "Next of kin"
        BENEFICIARY = "beneficiary", "Beneficiary"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="contacts")
    contact_type = models.CharField(
        max_length=20, choices=ContactType.choices, default=ContactType.EMERGENCY
    )

    full_name = models.CharField(max_length=150)
    relationship = models.CharField(max_length=50, blank=True)
    phone = models.CharField(max_length=20)
    alternate_phone = models.CharField(max_length=20, blank=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "employee_contact"
        ordering = ["employee_id", "contact_type", "-is_primary"]
        indexes = [
            models.Index(fields=["employee", "contact_type"]),
            models.Index(fields=["tenant"]),
        ]
        constraints = [
            # One primary per contact type. Two "primary" emergency contacts means
            # whoever reads the record picks one, which is not a decision to leave
            # to the moment somebody needs it.
            models.UniqueConstraint(
                fields=["employee", "contact_type"],
                condition=models.Q(is_primary=True),
                name="uniq_primary_contact_per_type",
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.contact_type})"
