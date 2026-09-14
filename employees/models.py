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


# ------------------------------------------------------------- work schedules


class WorkSchedule(AuditedModel, TenantScopedModel):
    """The employee's ordinary working pattern, effective-dated.

    This is what decides, for one person on one date, whether a day is an ordinary
    working day — and that single question drives four different payments: whether a
    public holiday is paid when not worked (BCEA s18), which Sunday multiplier applies
    (s16 pays double time only when Sunday is NOT ordinarily worked), how many days a
    period of leave consumes, and what a day of notice is worth.

    ``works_over_27_hours_week`` is the **SD7 rate band selector**, and it is a
    declared boolean rather than a threshold this system computes. That matters:
    ``employees/remuneration.py`` first derived the band by comparing hours against a
    literal 27, the no-hard-coded-rate guard caught it, and the honest answer was that
    nobody had confirmed the threshold is still live (D-105). The workbook's answer is
    better than either — the employer states which band the person is in, the gazette
    keeps its own threshold, and no figure needs to live in code at all (D-110).

    ``cycle_length_days`` is 7 for an ordinary week and 14 for a rotating fortnight.
    Anything else is allowed by the column and unexercised by the product.
    """

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="schedules")
    schedule_name = models.CharField(max_length=80, default="Standard")

    days_per_week = models.DecimalField(max_digits=4, decimal_places=2, default=5)
    ordinary_hours_per_week = models.DecimalField(max_digits=5, decimal_places=2, default=45)
    works_over_27_hours_week = models.BooleanField(
        default=True,
        help_text=(
            "The SD7 rate band, declared rather than computed. The gazette owns the "
            "threshold; this says which side of it the employee is on."
        ),
    )
    cycle_length_days = models.SmallIntegerField(
        default=7, help_text="7 for a weekly pattern, 14 for a rotating fortnight."
    )

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means current."
    )

    class Meta:
        db_table = "work_schedule"
        ordering = ["employee_id", "-effective_from"]
        indexes = [
            models.Index(fields=["employee", "-effective_from"]),
            models.Index(fields=["tenant"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "effective_from"], name="uniq_schedule_start_per_employee"
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="work_schedule_period_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_length_days__gte=1, cycle_length_days__lte=31),
                name="work_schedule_cycle_is_a_cycle",
            ),
            models.CheckConstraint(
                condition=models.Q(days_per_week__gt=0, days_per_week__lte=7),
                name="work_schedule_days_per_week_is_a_week",
            ),
            ExclusionConstraint(
                name="work_schedule_no_overlapping_periods",
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
        return f"{self.schedule_name} from {self.effective_from}"

    @property
    def hours_band(self) -> str:
        """The ``minimum_wage_rate`` band this schedule selects."""
        from statutory.models import MinimumWageRate

        if self.works_over_27_hours_week:
            return MinimumWageRate.HoursBand.GT_27
        return MinimumWageRate.HoursBand.LTE_27


class WorkScheduleDay(AuditedModel, TenantScopedModel):
    """One day of a schedule's cycle.

    ``ordinary_hours`` is stored **net of the unpaid break** rather than computed from
    the times, and the two are allowed to disagree. An employer who says 08:00 to
    17:00 with a 60-minute break and 8 ordinary hours is describing the ordinary case;
    one who says 8.5 has an arrangement, and the stored figure is what the employee
    agreed to. Recomputing would overwrite that silently.
    """

    work_schedule = models.ForeignKey(WorkSchedule, on_delete=models.CASCADE, related_name="days")
    cycle_day = models.SmallIntegerField(
        help_text="0 to cycle_length_days - 1. For a 7-day cycle, 0 is Monday."
    )
    is_working_day = models.BooleanField(default=True)

    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    unpaid_break_minutes = models.SmallIntegerField(default=60)
    ordinary_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, help_text="Net of the unpaid break."
    )

    class Meta:
        db_table = "work_schedule_day"
        ordering = ["work_schedule_id", "cycle_day"]
        indexes = [models.Index(fields=["tenant"])]
        constraints = [
            models.UniqueConstraint(
                fields=["work_schedule", "cycle_day"], name="uniq_cycle_day_per_schedule"
            ),
            models.CheckConstraint(
                condition=models.Q(ordinary_hours__gte=0, ordinary_hours__lte=24),
                name="work_schedule_day_hours_are_a_day",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_day__gte=0), name="work_schedule_day_is_not_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(unpaid_break_minutes__gte=0),
                name="work_schedule_day_break_is_not_negative",
            ),
            # A non-working day with hours on it is the contradiction that makes a
            # public holiday or a Sunday resolve two ways at once.
            models.CheckConstraint(
                condition=models.Q(is_working_day=True) | models.Q(ordinary_hours=0),
                name="work_schedule_day_off_has_no_hours",
            ),
        ]

    def __str__(self):
        return f"Day {self.cycle_day}: {self.ordinary_hours}h"


# ------------------------------------------------------------------ tax profile


class EmployeeTaxProfile(AuditedModel, TenantScopedModel):
    """Tax identity and directives, effective-dated.

    Effective-dated because a directive or a medical dependant count changes mid-year
    and the March payslip must still reproduce March.

    **``nature_of_person`` is the SARS code that decides how the IRP5 reads**, not a
    description. A is an individual with a South African ID, B one without, C a
    director. It defaults from ``employee.id_type`` because the answer is already on
    the record, and an employer asked to choose a letter will guess.

    **A director uses the ordinary tax tables.** The flat 25% director rate was
    repealed in 2017, and it is written here because it is the single most persistent
    piece of out-of-date South African payroll folklore — a future reader reaching for
    a directors' special case should find this line first.

    ``is_uif_exempt`` and ``is_sdl_exempt`` are declared flags with reasons, not
    computed. The UIF exemption for an employee working under 24 hours a month is a
    statutory threshold, and the same argument as the wage band applies (D-110): the
    employer states the fact, the statute keeps the number, and nothing needs to be
    hard-coded to ask the question.
    """

    class TaxStatus(models.TextChoices):
        STANDARD = "standard", "Standard tables"
        DIRECTIVE_FIXED_PCT = "directive_fixed_pct", "Directive — fixed percentage"
        DIRECTIVE_FIXED_AMOUNT = "directive_fixed_amount", "Directive — fixed amount"
        EXEMPT = "exempt", "Exempt"
        FOREIGN = "foreign", "Foreign"

    class NatureOfPerson(models.TextChoices):
        INDIVIDUAL_WITH_ID = "A", "A — individual with a South African ID"
        INDIVIDUAL_WITHOUT_ID = "B", "B — individual without a South African ID"
        DIRECTOR = "C", "C — director of a private company"

    class UifExemptReason(models.TextChoices):
        UNDER_24_HOURS = "under_24_hours_month", "Works under 24 hours a month"
        FOREIGN_REPATRIATION = "foreign_repatriation", "Foreign national to be repatriated"
        LEARNER = "learner", "Learner under the Skills Development Act"
        PUBLIC_SERVANT = "public_servant", "National or provincial public servant"

    #: The two statuses that require a directive on file.
    DIRECTIVE_STATUSES = {TaxStatus.DIRECTIVE_FIXED_PCT, TaxStatus.DIRECTIVE_FIXED_AMOUNT}

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="tax_profiles")

    tax_reference_number = models.CharField(
        max_length=15,
        blank=True,
        help_text="Ten-digit SARS number. Often genuinely absent below the threshold.",
    )
    tax_status = models.CharField(
        max_length=30, choices=TaxStatus.choices, default=TaxStatus.STANDARD
    )
    directive_number = models.CharField(max_length=30, blank=True)
    directive_percentage = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True
    )
    directive_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    directive_valid_to = models.DateField(null=True, blank=True)

    medical_scheme_members = models.SmallIntegerField(
        default=0, help_text="Main member plus dependants, for the medical tax credit."
    )

    is_uif_exempt = models.BooleanField(default=False)
    uif_exempt_reason = models.CharField(max_length=60, choices=UifExemptReason.choices, blank=True)
    is_sdl_exempt = models.BooleanField(default=False)

    nature_of_person = models.CharField(
        max_length=1,
        choices=NatureOfPerson.choices,
        default=NatureOfPerson.INDIVIDUAL_WITH_ID,
        help_text="The SARS code on the IRP5. Defaults from the employee's id_type.",
    )

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(null=True, blank=True)

    audit_sensitive_fields = ("tax_reference_number", "directive_number")

    class Meta:
        db_table = "employee_tax_profile"
        ordering = ["employee_id", "-effective_from"]
        indexes = [
            models.Index(fields=["employee", "-effective_from"]),
            models.Index(fields=["tenant"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "effective_from"], name="uniq_tax_profile_start_per_employee"
            ),
            models.CheckConstraint(
                condition=models.Q(medical_scheme_members__gte=0),
                name="tax_profile_medical_members_not_negative",
            ),
            models.CheckConstraint(
                condition=~models.Q(tax_status="directive_fixed_pct")
                | models.Q(directive_percentage__isnull=False),
                name="tax_profile_pct_directive_has_a_percentage",
            ),
            models.CheckConstraint(
                condition=~models.Q(tax_status="directive_fixed_amount")
                | models.Q(directive_amount__isnull=False),
                name="tax_profile_amount_directive_has_an_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(is_uif_exempt=False) | ~models.Q(uif_exempt_reason=""),
                name="tax_profile_uif_exemption_has_a_reason",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="tax_profile_period_ordered",
            ),
            ExclusionConstraint(
                name="employee_tax_profile_no_overlapping_periods",
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
        return f"Tax profile from {self.effective_from} ({self.tax_status})"

    @staticmethod
    def nature_from_id_type(id_type: str) -> str:
        """A for a South African ID, B for anything else.

        C (director) is never derived: being a director is a fact about the person's
        office, not about their identity document, and nothing on ``employee`` says it.
        """
        if id_type == Employee.IdType.SA_ID:
            return EmployeeTaxProfile.NatureOfPerson.INDIVIDUAL_WITH_ID
        return EmployeeTaxProfile.NatureOfPerson.INDIVIDUAL_WITHOUT_ID

    def clean(self):
        super().clean()

        if self.tax_status in self.DIRECTIVE_STATUSES and not self.directive_number:
            raise ValidationError(
                {
                    "directive_number": (
                        "A directive status needs the directive number. SARS issues it "
                        "per employee per year, and it goes on the IRP5."
                    )
                }
            )
        if (
            self.tax_status == self.TaxStatus.DIRECTIVE_FIXED_PCT
            and self.directive_percentage is None
        ):
            raise ValidationError(
                {"directive_percentage": "A fixed-percentage directive needs its percentage."}
            )
        if (
            self.tax_status == self.TaxStatus.DIRECTIVE_FIXED_AMOUNT
            and self.directive_amount is None
        ):
            raise ValidationError(
                {"directive_amount": "A fixed-amount directive needs its amount."}
            )
        if self.is_uif_exempt and not self.uif_exempt_reason:
            raise ValidationError(
                {
                    "uif_exempt_reason": (
                        "A UIF exemption needs its reason. The UI-19 asks for it, and "
                        "an unexplained exemption is the one an inspector opens with."
                    )
                }
            )


# ------------------------------------------------------------- bank accounts


class EmployeeBankAccount(AuditedModel, TenantScopedModel):
    """Where the employee's pay goes. Effective-dated, one live account at a time.

    Same encryption shape as ``employer_bank_account``: the number is encrypted and
    unqueryable, ``_last4`` is what a person recognises, and ``_hash`` is a keyed HMAC
    scoped to the tenant (D-95).

    **The hash is beyond sheet 02, and the reason is ghost employees** (D-111). Several
    "employees" paid into one bank account is the classic payroll fraud in contract
    cleaning, which is half this product's market, and without a comparable column
    there is no way to ask the question at all. It is a **signal, not a block**:
    spouses and families legitimately share an account, and a system that refused the
    second one would be wrong more often than right.

    ``payment_method`` of ``cash`` is a first-class option rather than an omission. A
    domestic employer paying a weekly wage in cash still owes a payslip under BCEA
    s33, and refusing to record the arrangement would push them off the product rather
    than into compliance.
    """

    class PaymentMethod(models.TextChoices):
        EFT = "eft", "Electronic transfer"
        CASH = "cash", "Cash"
        CHEQUE = "cheque", "Cheque"

    class AccountType(models.TextChoices):
        CURRENT = "current", "Current or cheque"
        SAVINGS = "savings", "Savings"
        TRANSMISSION = "transmission", "Transmission"

    class HolderRelationship(models.TextChoices):
        SELF = "self", "The employee"
        SPOUSE = "spouse", "Spouse"
        OTHER = "other", "Someone else"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="bank_accounts")

    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.EFT
    )
    bank = models.ForeignKey(
        "statutory.Bank",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="employee_accounts",
        help_text="NULL when the employee is paid in cash.",
    )
    branch_code = models.CharField(max_length=10, blank=True)

    account_number = EncryptedCharField(max_plaintext_length=30, blank=True, default="")
    account_number_last4 = models.CharField(max_length=4, blank=True, editable=False)
    account_number_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        db_index=True,
        help_text="Keyed HMAC, scoped to the tenant. Finds several employees on one account.",
    )
    account_type = models.CharField(max_length=20, choices=AccountType.choices, blank=True)
    account_holder_name = models.CharField(max_length=150, blank=True)
    account_holder_relationship = models.CharField(
        max_length=30, choices=HolderRelationship.choices, default=HolderRelationship.SELF
    )
    third_party_consent_file = models.ForeignKey(
        "core.FileObject",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Written consent, required when the account is not the employee's own.",
    )

    active_from = models.DateField(db_index=True)
    active_to = models.DateField(null=True, blank=True, help_text="Exclusive. NULL means live.")
    is_verified = models.BooleanField(
        default=False, help_text="Branch code and account length checks passed."
    )

    audit_sensitive_fields = ("account_number", "account_number_last4", "account_number_hash")

    class Meta:
        db_table = "employee_bank_account"
        ordering = ["employee_id", "-active_from"]
        indexes = [
            models.Index(fields=["employee", "-active_from"]),
            models.Index(fields=["tenant", "account_number_hash"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(active_to__isnull=True)
                | models.Q(active_to__gt=models.F("active_from")),
                name="employee_bank_account_period_ordered",
            ),
            # An EFT with no account number is a payment that cannot be made, captured
            # as though it could. The bank file would simply skip the employee.
            #
            # The check is on the LAST4 COMPANION, not on the encrypted column, and
            # that is not a workaround. ``EncryptedCharField`` refused the constraint
            # outright — Fernet ciphertext is non-deterministic, so comparing it to
            # anything is meaningless, and the field says so rather than letting a
            # constraint that can never fire look like protection. The companion is
            # plain text, derived from the number on every save, and empty exactly
            # when the number is.
            models.CheckConstraint(
                condition=~models.Q(payment_method="eft") | ~models.Q(account_number_last4=""),
                name="employee_bank_account_eft_has_a_number",
            ),
            ExclusionConstraint(
                name="employee_bank_account_no_overlapping_windows",
                expressions=[
                    (
                        DateRange("active_from", "active_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        if self.payment_method != self.PaymentMethod.EFT:
            return f"{self.get_payment_method_display()} from {self.active_from}"
        return f"****{self.account_number_last4} from {self.active_from}"

    def save(self, *args, **kwargs):
        if self.account_number:
            self.account_number_last4 = last4(self.account_number)
            self.account_number_hash = keyed_hash(
                self.account_number, scope=f"tenant:{self.tenant_id}"
            )
        else:
            self.account_number_last4 = ""
            self.account_number_hash = ""

        if kwargs.get("update_fields") is not None:
            update_fields = set(kwargs["update_fields"])
            if "account_number" in update_fields:
                update_fields |= {"account_number_last4", "account_number_hash"}
                kwargs["update_fields"] = sorted(update_fields)

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()

        if self.payment_method == self.PaymentMethod.EFT:
            if not self.account_number:
                raise ValidationError(
                    {"account_number": "An electronic payment needs an account number."}
                )
            if not self.bank_id:
                raise ValidationError({"bank": "An electronic payment needs a bank."})

        if (
            self.account_holder_relationship != self.HolderRelationship.SELF
            and self.third_party_consent_file_id is None
        ):
            raise ValidationError(
                {
                    "third_party_consent_file": (
                        "Paying into somebody else's account needs the employee's "
                        "written consent on file. BCEA s34 limits what may be done "
                        "with an employee's wages, and a verbal arrangement is what "
                        "this dispute always turns out to have been."
                    )
                }
            )


# ------------------------------------------------------- leave, deductions, notes


class EmployeeLeaveEntitlement(AuditedModel, TenantScopedModel):
    """Where an employee's leave departs from the statutory minimum.

    Most employees have no row here at all, and that is the point: absence means
    the sectoral rule set applies untouched. A row exists only where this employer
    gives this person something different — 21 days instead of 15, an upfront
    annual grant instead of monthly accrual, a carry-over the statute would forfeit.

    **Additive by default, replacing only when told.** ``additional_days_per_cycle``
    sits *on top of* the statutory figure, so a rule set change in March still
    reaches an employee who was given three extra days. ``replaces_statutory``
    flips that: the total is then ``total_days_per_cycle_override`` and the rule set
    is ignored, which is what a contract stating a flat entitlement needs. The two
    must not be confused — an additive 21 on top of a statutory 15 is 36 days, and
    that is how an employer accidentally triples its leave liability. The CHECK
    makes the replacing form state its total.

    **Nothing here may go below the statute.** The BCEA is a floor and a contract
    cannot contract out of it, so a replacing total under the rule set figure is
    refused at ``clean()`` rather than stored — unlike a below-minimum wage, which
    is stored with an acknowledgement (D-108) because an employer correcting a typo
    must be able to see the bad value on screen. Leave has no such workflow: there
    is no partial capture to protect, and a stored under-entitlement silently
    underpays every leave day for years.

    **Effective-dated like everything else** (invariant 2). Raising someone's leave
    in July must not retrospectively change what accrued in March.
    """

    class AccrualMethod(models.TextChoices):
        MONTHLY = "monthly", "Monthly, straight line"
        PER_DAYS_WORKED = "per_days_worked", "One day per 17 days worked"
        PER_HOURS_WORKED = "per_hours_worked", "One hour per 17 hours worked"
        UPFRONT_ANNUAL = "upfront_annual", "Granted in full at the start of the cycle"

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="leave_entitlements"
    )
    leave_type = models.ForeignKey(
        "leave.LeaveType", on_delete=models.PROTECT, related_name="entitlements"
    )

    additional_days_per_cycle = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=0,
        help_text="Over and above the statutory figure. Additive.",
    )
    replaces_statutory = models.BooleanField(
        default=False,
        help_text="TRUE means the override IS the total, not an addition to it.",
    )
    total_days_per_cycle_override = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="The whole entitlement. Required when replaces_statutory is TRUE.",
    )

    accrual_method = models.CharField(
        max_length=30, choices=AccrualMethod.choices, default=AccrualMethod.MONTHLY
    )
    carry_over_max_days = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="NULL means the statutory forfeiture rule applies.",
    )

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means current."
    )

    class Meta:
        db_table = "employee_leave_entitlement"
        ordering = ["employee_id", "leave_type_id", "-effective_from"]
        indexes = [models.Index(fields=["employee", "leave_type", "-effective_from"])]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "leave_type", "effective_from"],
                name="uniq_entitlement_start_per_employee_type",
            ),
            models.CheckConstraint(
                condition=models.Q(replaces_statutory=False)
                | models.Q(total_days_per_cycle_override__isnull=False),
                name="entitlement_replacement_states_its_total",
            ),
            models.CheckConstraint(
                condition=models.Q(additional_days_per_cycle__gte=0),
                name="entitlement_addition_is_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(total_days_per_cycle_override__isnull=True)
                | models.Q(total_days_per_cycle_override__gte=0),
                name="entitlement_total_is_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(carry_over_max_days__isnull=True)
                | models.Q(carry_over_max_days__gte=0),
                name="entitlement_carry_over_is_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    accrual_method__in=[
                        "monthly",
                        "per_days_worked",
                        "per_hours_worked",
                        "upfront_annual",
                    ]
                ),
                name="entitlement_accrual_method_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="entitlement_period_ordered",
            ),
            # Two entitlements in force for one leave type means two answers to
            # "how many days does this person get", decided by row order. The unique
            # on the start date only stops two rows BEGINNING on one day.
            ExclusionConstraint(
                name="employee_leave_entitlement_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("effective_from", "effective_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                    ("leave_type", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.leave_type_id} for {self.employee_id} from {self.effective_from}"

    def clean(self):
        super().clean()

        if not self.replaces_statutory and self.total_days_per_cycle_override is not None:
            raise ValidationError(
                {
                    "total_days_per_cycle_override": (
                        "An additive entitlement has no total of its own — the total is "
                        "the statutory figure plus additional_days_per_cycle. Set "
                        "replaces_statutory if this figure is meant to BE the whole "
                        "entitlement."
                    )
                }
            )

        if self.replaces_statutory and self.additional_days_per_cycle:
            raise ValidationError(
                {
                    "additional_days_per_cycle": (
                        "A replacing entitlement states the whole figure, so there is "
                        "nothing to add it to. One or the other, never both."
                    )
                }
            )


class EmployeeRecurringComponent(AuditedModel, TenantScopedModel):
    """A payslip line that repeats every period without being re-entered.

    A transport allowance, a loan repayment, an accommodation deduction, a union
    subscription. It points at a ``payroll_component`` for *what* it is — and how it
    is taxed, and which SARS code it lands on — and carries only *how much* and
    *for whom*.

    **Amount or percentage, never neither.** A fixed rand figure or a percentage of
    basic, enforced by CHECK. Both together is permitted by the constraint and
    refused by ``clean()``: the constraint can only see the row, and two figures on
    one line is an ambiguity rather than a contradiction, so it belongs where the
    message can explain itself.

    **``balance_outstanding`` is the loan half and it is a running figure, not a
    ledger.** It is decremented as each run finalises. It is a cache in the sense of
    invariant 3 — rebuildable from the payslip lines that paid it down — and the
    ledger is those lines, not this column. Never "fix" a loan by editing it.

    **BCEA s34 is why ``written_consent_file_id`` exists.** An employer may not
    deduct from wages without the employee's written consent except where a statute
    or court order says so, and "he agreed" is what every one of these disputes
    turns out to have been. The consent is a file on record, and ``clean()``
    requires it for a deduction that is not statutory. The section also caps what
    may be deducted, which is ``total_deduction_cap_pct`` — a *per-component* limit,
    such as the ten percent an accommodation deduction may not exceed. The
    across-all-components s34 limit is a payroll-run check and not this row's job:
    no single row can see the total, which is exactly how an employer ends up with
    four individually legal deductions that together take three quarters of a wage.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="recurring_components"
    )
    payroll_component = models.ForeignKey(
        "employers.PayrollComponent", on_delete=models.PROTECT, related_name="employee_lines"
    )

    amount = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True, help_text="Fixed per period."
    )
    percentage_of_basic = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True, help_text="Alternative to amount."
    )

    balance_outstanding = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Loans and advances. Decremented as each run finalises.",
    )
    total_deduction_cap_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="BCEA s34 per-component ceiling, e.g. 10% for accommodation.",
    )

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means current."
    )

    written_consent_file = models.ForeignKey(
        "core.FileObject",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="BCEA s34 requires written consent for most deductions.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "employee_recurring_component"
        ordering = ["employee_id", "-effective_from"]
        indexes = [
            models.Index(fields=["employee", "is_active"]),
            models.Index(fields=["tenant", "payroll_component"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__isnull=False)
                | models.Q(percentage_of_basic__isnull=False),
                name="recurring_component_states_an_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__isnull=True) | models.Q(amount__gte=0),
                name="recurring_component_amount_is_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(percentage_of_basic__isnull=True)
                | models.Q(percentage_of_basic__gte=0),
                name="recurring_component_percentage_is_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(balance_outstanding__isnull=True)
                | models.Q(balance_outstanding__gte=0),
                name="recurring_component_balance_is_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(total_deduction_cap_pct__isnull=True)
                | models.Q(total_deduction_cap_pct__gt=0, total_deduction_cap_pct__lte=100),
                name="recurring_component_cap_is_a_percentage",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="recurring_component_period_ordered",
            ),
            # Two live rows for the same component means the line is applied twice —
            # the employee is charged the deduction, or paid the allowance, double.
            ExclusionConstraint(
                name="employee_recurring_component_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("effective_from", "effective_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                    ("payroll_component", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.payroll_component_id} for {self.employee_id} from {self.effective_from}"

    def clean(self):
        super().clean()

        if self.amount is not None and self.percentage_of_basic is not None:
            raise ValidationError(
                {
                    "percentage_of_basic": (
                        "A line is a fixed amount or a percentage of basic, not both. "
                        "Two figures on one line means the payslip picks one and "
                        "nobody can tell which."
                    )
                }
            )

        component = self.payroll_component if self.payroll_component_id else None
        if component is None:
            return

        is_deduction = component.component_type == component.ComponentType.DEDUCTION

        # BCEA s34(1) permits a deduction without consent only where a law, court
        # order, arbitration award or collective agreement requires it. There is no
        # column for that and sheet 02 does not add one, so it is DERIVED from the
        # calculation method: a deduction computed from reference data by a statute
        # is a statutory one — PAYE and UIF_EE. ACCOM_DED and ADVANCE_DED are both
        # FIXED and both need consent, which is exactly the distinction s34 draws.
        # `is_system` is the wrong signal and worth naming: all four are system
        # components, and two of them still require the employee's signature.
        requires_consent = (
            is_deduction and component.calculation_method != component.CalculationMethod.STATUTORY
        )

        if requires_consent and self.written_consent_file_id is None:
            raise ValidationError(
                {
                    "written_consent_file": (
                        f"Deducting {component.code} needs the employee's written "
                        "consent on file. BCEA s34(1) permits a deduction without it "
                        "only where a statute, court order or collective agreement "
                        "requires one."
                    )
                }
            )

        if self.balance_outstanding is not None and not is_deduction:
            raise ValidationError(
                {
                    "balance_outstanding": (
                        "Only a deduction runs a balance down. An earning with a "
                        "balance is a loan recorded the wrong way round."
                    )
                }
            )


class EmployeeNote(AuditedModel, TenantScopedModel):
    """A dated note on the employee's file. Not discipline, and not a payroll input.

    Training attended, a conversation held, a commendation, a pattern of lateness
    worth recording before it becomes a case. Formal discipline is its own domain
    with its own evidence and appeal trail (P9); this is the file note that often
    precedes it, and keeping them apart matters — a note is not a warning, and
    treating it as one at the CCMA fails.

    **``is_confidential`` hides the note from self-service, and from nothing else.**
    It is a visibility flag, not a security boundary: the employer's own staff can
    read it, and a subject access request under POPIA s23 reaches it like any other
    personal information. Anyone writing one should assume the employee will
    eventually read it, because they are entitled to.

    Append-only in spirit rather than by trigger: the audit trail records edits, and
    the note is evidence of what was thought at the time.
    """

    class Category(models.TextChoices):
        GENERAL = "general", "General"
        PERFORMANCE = "performance", "Performance"
        TRAINING = "training", "Training"
        CONVERSATION = "conversation", "Conversation"
        ATTENDANCE = "attendance", "Attendance"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="notes")

    note_date = models.DateField(default=datetime.date.today, db_index=True)
    category = models.CharField(max_length=40, choices=Category.choices, default=Category.GENERAL)
    subject = models.CharField(max_length=150, blank=True)
    body = models.TextField()
    is_confidential = models.BooleanField(
        default=False, help_text="Hidden from employee self-service. Not a security boundary."
    )

    class Meta:
        db_table = "employee_note"
        ordering = ["-note_date", "-id"]
        indexes = [models.Index(fields=["employee", "-note_date"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    category__in=[
                        "general",
                        "performance",
                        "training",
                        "conversation",
                        "attendance",
                    ]
                ),
                name="employee_note_category_is_known",
            ),
            models.CheckConstraint(
                condition=~models.Q(body=""), name="employee_note_body_is_not_empty"
            ),
        ]

    def __str__(self):
        return f"{self.note_date} {self.category} for {self.employee_id}"
