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

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Now

from core.audit import AuditedModel
from core.db.fields import EncryptedCharField, keyed_hash, last4
from core.models import TenantScopedModel
from employees import identity
from employers.models import PROVINCE_CODES
from statutory.models import DateRange


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


# ---------------------------------------------------------------- engagements


class EmployeeEngagement(AuditedModel, TenantScopedModel):
    """One period of employment. A re-hire is a second row, never an edited first.

    **Service length is computed from this table, and that is the whole reason it
    exists.** Notice periods, severance, annual leave accrual and the BCEA's
    six-month thresholds all count continuous service, and an employee who left in
    2027 and came back in 2029 has two periods, not one long one. Overwriting the
    first engagement's dates on re-hire would silently grant them four years of
    accrued service they never had — and every downstream figure would look
    plausible.

    ``is_current`` is a cached flag with a partial unique behind it: at most one
    current engagement per employee. It is not merely "termination_date is NULL",
    because a future-dated termination is captured in advance and the employee is
    still currently employed until it arrives.

    ``termination_reason_code`` decides more than reporting. Only ``retrenchment``
    triggers severance under BCEA s41, and the UI-19 declaration carries its own
    code — so this is a compliance field, not a note.
    """

    class ContractType(models.TextChoices):
        PERMANENT = "permanent", "Permanent"
        FIXED_TERM = "fixed_term", "Fixed term"
        TEMPORARY = "temporary", "Temporary"
        CASUAL = "casual", "Casual"
        PROJECT = "project", "Project"

    class TerminationReason(models.TextChoices):
        RESIGNATION = "resignation", "Resignation"
        DISMISSAL_MISCONDUCT = "dismissal_misconduct", "Dismissal — misconduct"
        DISMISSAL_INCAPACITY = "dismissal_incapacity", "Dismissal — incapacity"
        RETRENCHMENT = "retrenchment", "Retrenchment (operational requirements)"
        END_OF_CONTRACT = "end_of_contract", "End of fixed-term contract"
        RETIREMENT = "retirement", "Retirement"
        DEATH = "death", "Death"
        ABSCONDED = "absconded", "Absconded"
        MUTUAL_SEPARATION = "mutual_separation", "Mutual separation"

    #: BCEA s41: severance is due on dismissal for operational requirements, and on
    #: nothing else in this list. Named here so the termination engine reads the
    #: rule rather than restating it.
    SEVERANCE_REASONS = {TerminationReason.RETRENCHMENT}

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="engagements")
    engagement_number = models.SmallIntegerField(
        default=1, help_text="1 for the first engagement, 2 for a re-hire, and so on."
    )

    start_date = models.DateField(db_index=True, help_text="Date of engagement.")
    probation_end_date = models.DateField(null=True, blank=True)

    contract_type = models.CharField(
        max_length=30, choices=ContractType.choices, default=ContractType.PERMANENT
    )
    fixed_term_end_date = models.DateField(
        null=True, blank=True, help_text="Required for a fixed-term contract."
    )
    fixed_term_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="LRA s198B justification for employing on a fixed term.",
    )

    termination_date = models.DateField(
        null=True, blank=True, db_index=True, help_text="Last day of service."
    )
    termination_reason_code = models.CharField(
        max_length=40, choices=TerminationReason.choices, blank=True
    )
    termination_notes = models.TextField(blank=True)
    notice_given_date = models.DateField(null=True, blank=True)
    notice_worked = models.BooleanField(
        null=True, blank=True, help_text="FALSE triggers notice pay in the termination engine."
    )
    uif_status_code = models.CharField(
        max_length=10, blank=True, help_text="UI-19 reason code for the declaration."
    )

    is_current = models.BooleanField(
        default=True, help_text="At most one per employee. Not the same as 'not terminated'."
    )

    class Meta:
        db_table = "employee_engagement"
        ordering = ["employee_id", "-engagement_number"]
        indexes = [
            models.Index(fields=["tenant", "start_date"]),
            models.Index(fields=["termination_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "engagement_number"], name="uniq_engagement_number_per_employee"
            ),
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(is_current=True),
                name="uniq_current_engagement_per_employee",
            ),
            models.CheckConstraint(
                condition=models.Q(termination_date__isnull=True)
                | models.Q(termination_date__gte=models.F("start_date")),
                name="engagement_ends_on_or_after_it_starts",
            ),
            # Sheet 03 writes this as an implication: fixed_term implies an end date.
            # A fixed-term contract with no end date is not a fixed-term contract, and
            # LRA s198B turns an unterminated one into permanent employment after
            # three months - which is a liability created by a blank field.
            models.CheckConstraint(
                condition=~models.Q(contract_type="fixed_term")
                | models.Q(fixed_term_end_date__isnull=False),
                name="engagement_fixed_term_has_an_end_date",
            ),
            models.CheckConstraint(
                condition=models.Q(is_current=False) | models.Q(termination_date__isnull=True),
                name="engagement_current_means_not_yet_terminated",
            ),
            models.CheckConstraint(
                condition=models.Q(termination_date__isnull=True)
                | ~models.Q(termination_reason_code=""),
                name="engagement_termination_has_a_reason",
            ),
            models.CheckConstraint(
                condition=models.Q(engagement_number__gte=1),
                name="engagement_number_starts_at_one",
            ),
        ]

    def __str__(self):
        return f"Engagement {self.engagement_number} of {self.employee_id} from {self.start_date}"

    @property
    def triggers_severance(self) -> bool:
        """BCEA s41. Only operational requirements, and this is where that is stated."""
        return self.termination_reason_code in self.SEVERANCE_REASONS

    def service_days_to(self, on_date: datetime.date) -> int:
        """Days of service in THIS engagement up to a date, inclusive of both ends.

        This engagement only. Continuous service across a re-hire is a different
        question with a different answer, and conflating them is how a returning
        employee gets a notice period they have not earned.
        """
        end = self.termination_date or on_date
        end = min(end, on_date)
        if end < self.start_date:
            return 0
        return (end - self.start_date).days + 1

    def clean(self):
        super().clean()

        if self.termination_date and self.start_date and self.termination_date < self.start_date:
            raise ValidationError({"termination_date": "Employment cannot end before it started."})

        if self.contract_type == self.ContractType.FIXED_TERM and not self.fixed_term_end_date:
            raise ValidationError(
                {
                    "fixed_term_end_date": (
                        "A fixed-term contract needs an end date. Without one it is not "
                        "fixed-term, and LRA s198B can turn it into permanent employment "
                        "after three months."
                    )
                }
            )

        if self.termination_date and not self.termination_reason_code:
            raise ValidationError(
                {
                    "termination_reason_code": (
                        "A termination needs a reason. It decides whether severance is "
                        "due and what goes on the UI-19 — it is not a note."
                    )
                }
            )

        if self.is_current and self.termination_date:
            raise ValidationError(
                {
                    "is_current": (
                        "An engagement with a termination date is not the current one. "
                        "Capture the termination and let the engagement close."
                    )
                }
            )


class EmployeePosition(AuditedModel, TenantScopedModel):
    """Effective-dated job title, grade, workplace and reporting line.

    Never overwritten. A promotion inserts a row and closes the previous one, because
    ``job_grade`` selects the minimum wage row the employee is measured against — so
    a March payslip re-run in 2029 has to see the grade they held in March, not the
    one they were promoted into in July.

    ``site_assignment`` is D-47, and deliberately small: single-site hides the site
    column from the attendance grid entirely, multi-site exposes a per-day picker,
    and a day belongs to one site. An allocation table for splitting a day across
    sites was considered and rejected as disproportionate.
    """

    class SiteAssignment(models.TextChoices):
        SINGLE_SITE = "single_site", "One site"
        MULTI_SITE = "multi_site", "More than one site"

    class ChangeReason(models.TextChoices):
        NEW_ENGAGEMENT = "new_engagement", "New engagement"
        PROMOTION = "promotion", "Promotion"
        TRANSFER = "transfer", "Transfer"
        REDEPLOYMENT = "redeployment", "Redeployment"
        CORRECTION = "correction", "Correction"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="positions")
    engagement = models.ForeignKey(
        EmployeeEngagement, on_delete=models.CASCADE, related_name="positions"
    )

    job_title = models.CharField(max_length=120, help_text="Free text, as the employer says it.")
    job_grade = models.ForeignKey(
        "statutory.JobGrade",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="positions",
        help_text="Selects the applicable minimum wage row.",
    )
    workplace = models.ForeignKey(
        "employers.Workplace",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="positions",
    )
    site_assignment = models.CharField(
        max_length=20, choices=SiteAssignment.choices, default=SiteAssignment.SINGLE_SITE
    )
    reports_to_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="direct_reports"
    )
    occupational_level = models.CharField(
        max_length=40, blank=True, help_text="For EEA reporting if it is ever required."
    )

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means current."
    )
    change_reason = models.CharField(max_length=60, choices=ChangeReason.choices, blank=True)

    class Meta:
        db_table = "employee_position"
        ordering = ["employee_id", "-effective_from"]
        indexes = [
            models.Index(fields=["employee", "-effective_from"]),
            models.Index(fields=["tenant"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "effective_from"], name="uniq_position_start_per_employee"
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="employee_position_period_ordered",
            ),
            # Sheet 03 asks for an EXCLUDE over the half-open range per employee, and
            # this is it. The unique on (employee, effective_from) stops two rows
            # STARTING on one day; only the exclusion stops a row that starts inside
            # another's period — which is what a back-dated promotion does, and which
            # would make two grades apply at once with the ORM picking one by ordering.
            ExclusionConstraint(
                name="employee_position_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("effective_from", "effective_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.job_title} from {self.effective_from}"

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValidationError({"effective_to": "The end date must be after the start date."})

        if (
            self.site_assignment == self.SiteAssignment.SINGLE_SITE
            and self.workplace_id is None
            and self.engagement_id
        ):
            # A warning rather than a refusal: a domestic employer has no workplace
            # records at all, and the household IS the site. Refusing here would
            # block the simpler half of the market to serve the other half.
            return


# --------------------------------------------------------------- remuneration


class EmployeeRemuneration(AuditedModel, TenantScopedModel):
    """The employee's pay, effective-dated. The row in force is what payroll reads.

    **The three derived rates are stored, not computed on read**, and that is
    invariant 2 rather than an optimisation. Recomputing on read would recompute
    with today's ``hours_per_week`` and today's statutory factor, so a March 2026
    payslip re-run in 2029 would quietly differ from the one the employee was given.
    The derivation is in ``employees/rates.py``, pure and handed its inputs.

    **The captured basis is preserved exactly.** An employee captured at R4,500 a
    month has ``derived_monthly_rate`` of exactly 4500 — not 4500 converted to a week
    and back, which would land a few cents away from the figure on their contract.

    **``is_below_minimum`` is a flag, not a block**, and that is the workbook's call.
    The service function refuses unless somebody acknowledges it, and the
    acknowledging user is recorded — because "the system let me" is not a defence, and
    an employer who genuinely has a correction to make in the next five minutes must
    not be locked out of their own record while they make it.

    ``minimum_wage_rate`` points at the row the rate was measured against, so the
    question "which floor was this checked against, on the day it was captured" has a
    stored answer rather than a re-derivation against whatever is loaded now.
    """

    class PayBasis(models.TextChoices):
        HOURLY = "hourly", "Hourly"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        FORTNIGHTLY = "fortnightly", "Fortnightly"
        MONTHLY = "monthly", "Monthly"

    class ChangeReason(models.TextChoices):
        NEW_ENGAGEMENT = "new_engagement", "New engagement"
        ANNUAL_INCREASE = "annual_increase", "Annual increase"
        MINIMUM_WAGE_UPLIFT = "minimum_wage_uplift", "Minimum wage uplift"
        PROMOTION = "promotion", "Promotion"
        CORRECTION = "correction", "Correction"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="remuneration")
    engagement = models.ForeignKey(
        EmployeeEngagement, on_delete=models.CASCADE, related_name="remuneration"
    )
    pay_group = models.ForeignKey(
        "employers.PayGroup", on_delete=models.PROTECT, related_name="remuneration"
    )

    pay_basis = models.CharField(max_length=20, choices=PayBasis.choices, db_index=True)
    rate_amount = models.DecimalField(
        max_digits=14, decimal_places=4, help_text="As captured, in the unit of pay_basis."
    )

    derived_hourly_rate = models.DecimalField(
        max_digits=14,
        decimal_places=6,
        help_text="Normalised. The single input every calculator uses.",
    )
    derived_daily_rate = models.DecimalField(max_digits=14, decimal_places=6)
    derived_monthly_rate = models.DecimalField(max_digits=14, decimal_places=6)

    hours_per_day = models.DecimalField(max_digits=5, decimal_places=2, default=9)
    days_per_week = models.DecimalField(max_digits=4, decimal_places=2, default=5)
    hours_per_week = models.DecimalField(max_digits=5, decimal_places=2, default=45)

    minimum_wage_rate = models.ForeignKey(
        "statutory.MinimumWageRate",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="validated_remuneration",
        help_text="The floor this rate was measured against, as at capture.",
    )
    is_below_minimum = models.BooleanField(
        default=False, help_text="A flag, not a block. Allows the correction workflow."
    )
    below_minimum_ack_by_user = models.ForeignKey(
        "core.AppUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Who accepted a below-minimum rate. 'The system let me' is not a defence.",
    )

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means current."
    )
    change_reason = models.CharField(max_length=60, choices=ChangeReason.choices, blank=True)

    class Meta:
        db_table = "employee_remuneration"
        ordering = ["employee_id", "-effective_from"]
        indexes = [
            models.Index(fields=["employee", "-effective_from"]),
            models.Index(fields=["tenant", "pay_basis"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "effective_from"],
                name="uniq_remuneration_start_per_employee",
            ),
            models.CheckConstraint(
                condition=models.Q(rate_amount__gt=0), name="remuneration_rate_is_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    pay_basis__in=["hourly", "daily", "weekly", "fortnightly", "monthly"]
                ),
                name="remuneration_pay_basis_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="remuneration_period_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(hours_per_week__gt=0, days_per_week__gt=0, hours_per_day__gt=0),
                name="remuneration_working_pattern_is_positive",
            ),
            # Two rates in force at once means two answers to "what is this employee
            # paid", with the ORM picking one by row order. The unique on
            # (employee, effective_from) only stops two rows STARTING on one day; a
            # back-dated increase starts inside the open period and slips past it.
            ExclusionConstraint(
                name="employee_remuneration_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("effective_from", "effective_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.rate_amount} {self.pay_basis} from {self.effective_from}"

    @property
    def working_pattern(self):
        from employees.rates import WorkingPattern

        return WorkingPattern(
            hours_per_day=self.hours_per_day,
            days_per_week=self.days_per_week,
            hours_per_week=self.hours_per_week,
        )
