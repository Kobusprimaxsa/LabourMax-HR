"""Leave — phase P6, domain 6. ``leave_type`` arrives early, in P4.

Only ``leave_type`` is here. The rest of the domain — cycles, the transaction
ledger, applications, the accrual engine — is P6 and is listed in
``docs/PHASES.md``.

**Why this one table is out of phase.** ``employee_leave_entitlement`` is a P4
table and it is meaningless without something to point at: it records that *this
employee* gets more than the statute allows, or a different accrual method, for
*this kind of leave*. The alternatives were both worse than pulling one table
forward. Leaving the entitlement table out would have closed P4 with a hole in the
employee master file, and P6 would then have to revisit P4's migrations. Pointing
the FK at a string code would have put an unenforceable reference in the schema,
which is the failure ``PROTECT`` exists to prevent (D-127).

**A NULL tenant means shared, not orphaned** — the same shape as
``payroll_component`` (D-87). The platform stocks the statutory types and an
employer adds its own on top: a birthday day, a long-service day, study leave on
terms better than the BCEA's. A tenant reads its own rows and the platform's
together and writes only its own.

**No entitlement figure lives here.** The number of days is a *statutory* figure
and belongs in ``leave_rule_set`` with its citation, which is where P2 already put
it. What this table carries is the *shape* of a leave type: whether it accrues,
whether it is paid, whether it survives termination, how long its cycle runs.
``cycle_months`` is the one that looks like a rate and is not — 12 for annual, 36
for sick — because it is a property of the cycle rather than an amount of
anything, and the days that accrue inside it are still read from the rule set.

**``balance_source`` is what makes sub-types work.** Annual leave taken without
authorisation still consumes the annual balance, so ``ANNUAL_UNAUTHORISED`` points
at ``ANNUAL`` as its parent and draws on the parent's balance rather than keeping
its own. Without it, an employer recording unauthorised absence would hand the
employee back the day.
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.audit import AuditedModel
from core.models import AuditMixin, TenantScopedModel, TenantSharedModel
from statutory.models import DateRange


class LeaveType(AuditedModel, TenantSharedModel):
    """A kind of leave, and how it behaves. Not how much of it there is."""

    class Code(models.TextChoices):
        ANNUAL = "ANNUAL", "Annual leave"
        ANNUAL_UNAUTHORISED = "ANNUAL_UNAUTHORISED", "Annual leave — unauthorised"
        SICK = "SICK", "Sick leave"
        FAMILY_RESPONSIBILITY = "FAMILY_RESPONSIBILITY", "Family responsibility leave"
        MATERNITY = "MATERNITY", "Maternity leave"
        PARENTAL = "PARENTAL", "Parental leave"
        ADOPTION = "ADOPTION", "Adoption leave"
        UNPAID = "UNPAID", "Unpaid leave"
        STUDY = "STUDY", "Study leave"
        COMPASSIONATE = "COMPASSIONATE", "Compassionate leave"

    class BalanceSource(models.TextChoices):
        OWN = "own", "Keeps its own balance"
        PARENT = "parent", "Consumes the parent type's balance"
        NONE = "none", "Has no balance at all"

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)

    parent_leave_type = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sub_types",
        help_text="Lets a sub-type draw on a parent balance.",
    )
    balance_source = models.CharField(
        max_length=30,
        choices=BalanceSource.choices,
        default=BalanceSource.OWN,
    )

    is_paid = models.BooleanField(default=True)
    is_statutory = models.BooleanField(default=True)
    accrues = models.BooleanField(
        default=True, help_text="FALSE for unpaid and unauthorised leave."
    )
    cycle_months = models.SmallIntegerField(
        default=12, help_text="12 for annual and family responsibility, 36 for sick."
    )
    requires_evidence = models.BooleanField(default=False)
    payable_on_termination = models.BooleanField(
        default=False, help_text="TRUE only for annual leave — BCEA s40(b)."
    )
    reduces_pay_when_exhausted = models.BooleanField(
        default=True, help_text="Converts to unpaid once the balance is zero."
    )
    counts_as_service = models.BooleanField(
        default=True, help_text="Whether the period counts toward service length."
    )

    # D-127 said this column exists and the migration installs the lock that keys
    # on it; the column itself was never written, so every UPDATE and DELETE on
    # ANY leave_type row — a tenant's own included — failed inside the trigger with
    # `record "old" has no field "is_system"`. Migration 0002 adds it. The one test
    # that covered the area asserted `DatabaseError` and passed on the wrong error,
    # which is the argument for asserting on the message as well as the class.
    is_system = models.BooleanField(
        default=False, help_text="System types cannot be edited or deleted (D-93)."
    )

    colour_hex = models.CharField(max_length=7, default="#4A7C9E")
    display_order = models.SmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "leave_type"
        ordering = ["display_order", "code"]
        constraints = [
            # A shared row IS a system row, and a tenant's row is never one — the
            # same pair payroll_component carries, and for the same reason. Without
            # the first, a shared row with is_system false is readable by every
            # tenant and deletable by any of them, because a DELETE is checked
            # against the policy's USING clause only (D-93). Without the second, an
            # employer could mint a row of its own that it can then never edit.
            models.CheckConstraint(
                condition=models.Q(tenant__isnull=False) | models.Q(is_system=True),
                name="leave_type_shared_rows_are_system_rows",
            ),
            models.CheckConstraint(
                condition=models.Q(tenant__isnull=True) | models.Q(is_system=False),
                name="leave_type_system_rows_are_shared_rows",
            ),
            # Coalesce, because NULL = NULL is unknown in PostgreSQL and the shared
            # rows are exactly the ones with a NULL tenant — so a plain unique would
            # permit the platform to stock ANNUAL twice.
            models.UniqueConstraint(
                models.functions.Coalesce("tenant_id", models.Value(0)),
                "code",
                name="uniq_leave_type_code_per_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(balance_source__in=["own", "parent", "none"]),
                name="leave_type_balance_source_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_months__gt=0),
                name="leave_type_cycle_is_positive",
            ),
            # The colour lands in a leave calendar, a payslip legend and a PDF. A
            # value that is not a hex triplet renders as whatever the browser
            # guesses in one place and as black in the PDF, which reads as a bug in
            # the document rather than as a bad setting.
            models.CheckConstraint(
                condition=models.Q(colour_hex__regex=r"^#[0-9A-Fa-f]{6}$"),
                name="leave_type_colour_is_a_hex_triplet",
            ),
            # A type that draws on a parent must have one, and a type with no parent
            # cannot draw on one. Either half alone leaves a balance nobody can find.
            models.CheckConstraint(
                condition=~models.Q(balance_source="parent")
                | models.Q(parent_leave_type__isnull=False),
                name="leave_type_parent_balance_needs_a_parent",
            ),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def clean(self):
        super().clean()

        if self.parent_leave_type_id == self.pk and self.pk is not None:
            raise ValidationError({"parent_leave_type": "A leave type cannot be its own parent."})

        # One level only. A grandparent chain means resolving a balance walks an
        # arbitrary number of rows, and every accrual read pays for it.
        if self.parent_leave_type_id is not None:
            parent = self.parent_leave_type
            if parent.parent_leave_type_id is not None:
                raise ValidationError(
                    {
                        "parent_leave_type": (
                            f"{parent.code} already draws on another type. Sub-types are "
                            "one level deep, so point this at the type that owns the "
                            "balance."
                        )
                    }
                )

        # A shared type pointing at a tenant's row would put one employer's private
        # leave type in the catalogue every other employer reads — through the FK,
        # where no policy is looking. PROTECT then stops that tenant deleting their
        # own row, and the reason would be invisible to them.
        if self.tenant_id is None and self.parent_leave_type_id is not None:
            if self.parent_leave_type.tenant_id is not None:
                raise ValidationError(
                    {
                        "parent_leave_type": (
                            "A shared leave type cannot draw on one employer's own "
                            "type. Stock the parent in the catalogue first."
                        )
                    }
                )

        if self.balance_source == self.BalanceSource.NONE and self.accrues:
            raise ValidationError(
                {
                    "accrues": (
                        "A type with no balance cannot accrue into one. Unpaid and "
                        "unauthorised leave are recorded, not accrued."
                    )
                }
            )

        if self.payable_on_termination and not self.is_paid:
            raise ValidationError(
                {
                    "payable_on_termination": (
                        "Unpaid leave cannot be paid out on termination — there is no "
                        "rate to pay it at."
                    )
                }
            )


class LeaveEvidenceType(AuditedModel, AuditMixin):
    """The four ways sick leave can be evidenced — or not (P6 chunk 2, task 1).

    **Evidence gates PAY, not leave.** BCEA s23 does NOT make a certificate a
    condition of TAKING sick leave — it permits the employer to WITHHOLD PAY
    where the employee was absent more than the permitted consecutive days
    (or occasions in a window) and produces no certificate on request. So
    nothing in this codebase may refuse or block a leave application for
    want of evidence: this table decides ``is_paid``, and it decides which
    evidence would have made the day paid. Refusing the leave itself would
    impose a condition the Act does not — a leave application against this
    catalogue is TAKEN either way; only whether it is PAID depends on the
    evidence.

    **No tenant field, on purpose, exactly as sheet 02 gives it none.**
    Unlike ``leave_type`` (``TenantSharedModel``, extendable per employer),
    this table has no ``is_system`` column and no tenant-facing write path —
    it is pure reference data, the same shape as ``Sector`` or
    ``PublicHoliday``, and inherits none of the three tenant bases
    accordingly. ``core/db/rls.py::no_delete()`` guards it in the creating
    migration regardless, because ``leave_application`` — a
    ``TenantScopedModel`` under FORCE ROW LEVEL SECURITY — points at it, and
    D-76 already found what happens when a reference table a tenant table
    references has no such guard: a session with no tenant pinned deletes
    the row, the referencing table's own RLS hides the rows that would have
    protected it, and nothing raises.

    **``max_consecutive_days_without_note`` is BCEA s23(1)'s own THRESHOLD,
    never a literal.** Seeded from ``SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS``
    via ``statutory.resolve.parameter_value()`` — the same
    ``statutory_parameter`` mechanism D-100/D-101 already use for the s43
    minimum employment age, because this is the same SHAPE of figure: one
    citable statutory number, not a whole rule set, and not a property of
    the leave cycle the way ``cycle_months`` is. ``test_no_hardcoded_rates``
    would not catch a literal ``2`` here either — it is an ``int``, not a
    ``Decimal`` — so the rule is enforced by reading the reference, not by
    the scanner.

    **Scoped to ``SICK`` only, this chunk.** ``FAMILY_RESPONSIBILITY`` and
    ``ADOPTION`` also carry ``leave_type.requires_evidence = TRUE``
    (chunk 1), but neither has a graded evidence catalogue like sick leave's
    four variants — s27(4)'s "reasonable proof" is a single yes/no, not a
    doctor's-note-vs-self-certified spectrum with its own pay consequence.
    Building evidence-type rows for them without a cited shape to seed would
    be inventing structure sheet 02 does not ask for.
    """

    class Code(models.TextChoices):
        DOCTOR_NOTE = "DOCTOR_NOTE", "Doctor's note"
        CLINIC_NOTE = "CLINIC_NOTE", "Clinic note"
        NO_NOTE = "NO_NOTE", "No note produced"
        SELF_CERTIFIED = "SELF_CERTIFIED", "Self-certified"

    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="evidence_types"
    )
    code = models.CharField(max_length=40, choices=Code.choices)
    name = models.CharField(max_length=100)
    requires_attachment = models.BooleanField(default=False)
    is_paid_by_default = models.BooleanField(
        default=True,
        help_text="BCEA s23 allows pay to be withheld without a certificate in defined cases.",
    )
    max_consecutive_days_without_note = models.SmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "BCEA s23(1): beyond this many consecutive days with no certificate, pay "
            "may be withheld. Read from SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS via "
            "statutory.resolve, never invented here. NULL where the variant already "
            "carries its own proof and the threshold does not apply."
        ),
    )
    display_order = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "leave_evidence_type"
        ordering = ["leave_type_id", "display_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["leave_type", "code"], name="uniq_leave_evidence_type_per_leave_type"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    code__in=["DOCTOR_NOTE", "CLINIC_NOTE", "NO_NOTE", "SELF_CERTIFIED"]
                ),
                name="leave_evidence_type_code_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(max_consecutive_days_without_note__isnull=True)
                | models.Q(max_consecutive_days_without_note__gt=0),
                name="leave_evidence_type_threshold_is_positive",
            ),
        ]

    def __str__(self):
        return f"{self.leave_type_id} {self.code}"


class LeaveCycle(AuditedModel, TenantScopedModel):
    """One period of one leave type's balance, for one employee (P6 chunk 1).

    **Anchored to the employee's CURRENT engagement** (D-B). Cycle 1 starts on
    ``engagement.start_date``; cycle 2 starts exactly where cycle 1 ends, and
    so on. A re-hire is a NEW engagement row (D-103), so a re-hire starts
    fresh cycles from cycle 1 again — the old engagement's cycles are never
    touched again and never revived. Whatever they held stays exactly as it
    was: an honest record of a period of employment that ended, not a balance
    quietly folded into the new one. Consistent with
    ``employees.engagements.continuous_service_days()``, which already counts
    the current engagement only.

    ``cycle_end`` is EXCLUSIVE — the same half-open convention as
    ``EffectiveDatedModel`` everywhere else in this schema — and the EXCLUDE
    constraint below is what proves cycles never overlap for one employee and
    leave type (the D-131 lesson: a UNIQUE on the start date alone would stop
    two cycles beginning on the same day and do nothing about one back-dated
    into an open period).

    **Balance columns are a CACHE, derived from ``leave_transaction`` and
    never edited directly** (invariant 3) — see ``leave/balances.py``.
    Renamed from sheet 02's ``*_days`` to ``*_quantity`` (a deliberate
    deviation, recorded against D-C): a column literally named
    ``accrued_days`` holding HOURS for an employee whose entitlement accrues
    per hour worked is exactly the silent-conversion trap D-C exists to
    prevent, just spelled as a misleading name instead of a computed one.
    ``unit`` says which one this cycle is in, and it is fixed for the whole
    cycle — the accrual method that set it does not change mid-cycle.

    **There is no cap on carried leave** (D-D). Forfeiture is never
    automatic — the employer captures it by hand (chunk 3) — so
    ``forfeited_quantity`` stays zero until a human writes a forfeiture
    transaction, however many cycles have accumulated. P7's termination
    payout must pay what the ledger holds, not what a policy assumes should
    have been left behind.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        FORFEITED = "forfeited", "Forfeited"

    class Unit(models.TextChoices):
        DAYS = "days", "Days"
        HOURS = "hours", "Hours"

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.CASCADE, related_name="leave_cycles"
    )
    engagement = models.ForeignKey(
        "employees.EmployeeEngagement", on_delete=models.PROTECT, related_name="leave_cycles"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="cycles")

    cycle_number = models.SmallIntegerField(
        default=1, help_text="1, 2, 3... from the engagement's own start date."
    )
    cycle_start = models.DateField(db_index=True)
    cycle_end = models.DateField(db_index=True, help_text="Exclusive.")

    unit = models.CharField(max_length=10, choices=Unit.choices)

    entitlement_quantity = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=0,
        help_text="Full-cycle entitlement, statutory + contractual, in `unit`.",
    )
    accrued_quantity = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    taken_quantity = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    paid_out_quantity = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    adjusted_quantity = models.DecimalField(
        max_digits=8, decimal_places=3, default=0, help_text="Manual corrections, signed."
    )
    carried_in_quantity = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=0,
        help_text="Brought forward from the prior cycle.",
    )
    forfeited_quantity = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=0,
        help_text="Zero until an employer captures a forfeiture by hand (D-D). Never automatic.",
    )
    balance_quantity = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=0,
        help_text=(
            "Derived: the SUM of carried_in, accrued, adjusted, taken, paid_out and "
            "forfeited — a plain addition, not a subtraction, because taken, paid_out "
            "and forfeited are themselves stored NEGATIVE per leave_transaction's own "
            "sign convention. Subtracting a negative would double the effect."
        ),
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    computed_at = models.DateTimeField(default=timezone.now)
    is_stale = models.BooleanField(
        default=False, help_text="Set when a leave_transaction against this cycle changes."
    )

    class Meta:
        db_table = "leave_cycle"
        ordering = ["employee_id", "leave_type_id", "cycle_number"]
        indexes = [
            models.Index(fields=["tenant", "leave_type", "status"]),
            models.Index(fields=["cycle_end"]),
        ]
        constraints = [
            # DEVIATION FROM SHEET 02 (recorded against D-B): the workbook's own
            # unique is (employee, leave_type, cycle_number), with no engagement
            # in it. That is fine for an employee with one engagement and wrong
            # for a re-hire — decision B requires a re-hire to start again at
            # cycle 1, and a bare (employee, leave_type, cycle_number) unique
            # would collide with the FIRST engagement's own cycle 1 the moment
            # the second one tried to create it. Scoping to the engagement is
            # what "fresh cycles" actually requires.
            models.UniqueConstraint(
                fields=["engagement", "leave_type", "cycle_number"],
                name="uniq_leave_cycle_number_per_engagement_type",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_end__gt=models.F("cycle_start")),
                name="leave_cycle_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(unit__in=["days", "hours"]),
                name="leave_cycle_unit_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["open", "closed", "forfeited"]),
                name="leave_cycle_status_is_known",
            ),
            # The D-131 lesson, applied here: a UNIQUE on cycle_number stops two
            # cycles claiming the same ordinal, and does nothing about their
            # DATES actually overlapping if one were ever back-dated.
            ExclusionConstraint(
                name="leave_cycle_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("cycle_start", "cycle_end", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                    ("leave_type", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.employee_id} {self.leave_type_id} cycle {self.cycle_number}"


class LeaveTransaction(AuditedModel, TenantScopedModel):
    """The append-only ledger every leave balance is derived from (invariant 3).

    **Sign convention — stated once here, enforced by CHECK below:**

    - ``accrual``, ``opening_balance``: POSITIVE. Increases the balance.
    - ``taken``, ``payout``, ``forfeiture``: NEGATIVE. Decreases the balance.
    - ``adjustment``: either sign — a correction can go either way, which is
      exactly why its ``reason`` is mandatory: the sign alone never explains
      an adjustment the way it explains every other type.
    - ``reversal``: either sign at the DATABASE level (a CHECK cannot see the
      row it points at), but ``leave/ledger.py::reverse_transaction()``
      refuses one that is not the EXACT negation of ``reverses_transaction``.

    A sign that is only implied by convention is a bug generator — it
    decides whether somebody's balance goes up or down, silently, the moment
    someone gets the sign backwards. Stating it once, here, and enforcing it
    with a CHECK the database itself cannot be talked out of, is the whole
    point of writing this paragraph.

    **Append-only by TRIGGER**, not by application discipline —
    ``core/db/rls.py::append_only()``. P0 already learned that a ``REVOKE``
    does not bind the table owner the application connects as; a trigger is
    what actually holds, regardless of how the row is reached.

    **A correction is a REVERSAL, never a delete and never an edit**
    (invariant 4). ``reverses_transaction`` points at the row it corrects.
    Reversing a reversal is refused by ``leave/ledger.py`` — a reversal IS
    the correction, and there is nothing further to undo.

    **Two physical columns, ``days`` and ``hours``, exactly one populated per
    row — reconciled against sheet 02 in chunk 2 (D-170).** Chunk 1 first
    built this as a single ``quantity`` + a ``unit`` discriminator. Sheet 02
    instead names ``days`` NOT NULL and ``hours`` nullable ("populated for
    hourly-accrual employees") — read literally, EVERY row states a DAYS
    figure, which for an hourly-accrual employee can only be produced by
    converting their hours into a days-equivalent using the employee's own
    work schedule. That is precisely the silent, schedule-dependent,
    lossy conversion D-164 (decision C) exists to prevent — an hourly
    worker's leave "quietly becoming somebody's rounded guess" is D-164's own
    wording for exactly this failure mode. Reconciled by keeping BOTH
    columns NULLABLE instead: whichever field matches the unit the accrual
    that produced this row actually used is populated, and the other stays
    NULL — the data shape chunk 1 already committed to (one unit, never
    converted, never both), spelled with sheet 02's own column names instead
    of an enum. The one point of genuine conflict — sheet 02's ``days`` being
    NOT NULL — is decided in D-164's favour, a settled Kobus decision this
    specific reconciliation raises explicitly rather than silently keeps
    diverging from sheet 02 without saying so (see D-170 in DECISIONS.md).

    A CHECK enforces exactly one of the two is set
    (``leave_transaction_exactly_one_of_days_or_hours``), and the sign CHECK
    below tests the SET one — written with an explicit ``__isnull=False``
    guard on each branch, because PostgreSQL treats a CHECK expression that
    evaluates to NULL (not FALSE) as SATISFIED: a naive
    ``Q(transaction_type="accrual", hours__gt=0)`` branch, evaluated on a row
    where ``hours`` IS NULL, produces NULL rather than FALSE, and ORing that
    against another FALSE branch yields NULL for the whole expression — which
    PostgreSQL then treats as passing, exactly the wrong-signed row this
    CHECK exists to catch. ``LeaveCycle.clean()`` (called via
    ``leave/ledger.py::post_transaction()``) additionally refuses a row whose
    populated field does not match its own ``leave_cycle.unit`` — a second,
    independent guard against a caller passing the wrong field for the cycle
    it is posting against.

    ``leave_application`` is now a real FK (P6 chunk 2, task 0/4) — it was
    ``leave_application_id_ref``, a placeholder ``BigIntegerField``, through
    chunk 1. ``payroll_run_id_ref`` stays a placeholder, following the same
    house pattern ``core.TenantMembership.employee_id_ref`` and
    ``attendance_day.locked_by_payroll_run_id_ref`` use — it becomes a real
    FK to ``payroll_run`` in P7, set only for a payout processed in a run.
    """

    class TransactionType(models.TextChoices):
        ACCRUAL = "accrual", "Accrual"
        OPENING_BALANCE = "opening_balance", "Opening balance"
        TAKEN = "taken", "Taken"
        PAYOUT = "payout", "Payout"
        FORFEITURE = "forfeiture", "Forfeiture"
        ADJUSTMENT = "adjustment", "Adjustment"
        REVERSAL = "reversal", "Reversal"

    #: Types whose quantity must be POSITIVE, and types whose quantity must be
    #: NEGATIVE — read by ``leave/ledger.py`` so the sign rule is stated once
    #: and imported, not repeated. ADJUSTMENT and REVERSAL appear in neither:
    #: either sign is legitimate for both, for different reasons (see above).
    POSITIVE_TYPES = frozenset({TransactionType.ACCRUAL, TransactionType.OPENING_BALANCE})
    NEGATIVE_TYPES = frozenset(
        {TransactionType.TAKEN, TransactionType.PAYOUT, TransactionType.FORFEITURE}
    )

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="leave_transactions"
    )
    leave_cycle = models.ForeignKey(
        LeaveCycle, on_delete=models.PROTECT, related_name="transactions"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="transactions")

    transaction_date = models.DateField(db_index=True)
    transaction_type = models.CharField(
        max_length=30, choices=TransactionType.choices, db_index=True
    )
    days = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
        help_text=(
            "Signed. Populated when this row's own unit is DAYS. See the model "
            "docstring's sign convention and D-170's reconciliation against sheet 02."
        ),
    )
    hours = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Signed. Populated when this row's own unit is HOURS (D-170). Never both.",
    )

    leave_application = models.ForeignKey(
        "LeaveApplication",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="leave_transactions",
        help_text="Set by leave/authorisation.py's approve() and reverse_transaction() on cancel.",
    )
    payroll_run_id_ref = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Becomes a real FK to payroll_run in P7. Set for a payout processed in a run.",
    )
    reverses_transaction = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversed_by",
        help_text="Set only on a reversal — the row it corrects.",
    )

    calculation_basis = models.CharField(
        max_length=40,
        blank=True,
        help_text=(
            "monthly_1_25 | monthly_1_50 | per_17_days | per_17_hours | "
            "per_26_days_first_6m | manual"
        ),
    )
    reason = models.CharField(max_length=255, blank=True, help_text="Mandatory for adjustments.")

    class Meta:
        db_table = "leave_transaction"
        ordering = ["-transaction_date", "-id"]
        indexes = [
            models.Index(fields=["employee", "leave_type", "transaction_date"]),
            models.Index(fields=["tenant", "transaction_type"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    transaction_type__in=[
                        "accrual",
                        "opening_balance",
                        "taken",
                        "payout",
                        "forfeiture",
                        "adjustment",
                        "reversal",
                    ]
                ),
                name="leave_transaction_type_is_known",
            ),
            # Exactly one of days/hours — D-170's reconciliation. Both NULL and
            # both set are refused equally; which one is legitimate depends on
            # the accrual that produced this row, never on this table alone.
            models.CheckConstraint(
                condition=(
                    models.Q(days__isnull=False, hours__isnull=True)
                    | models.Q(days__isnull=True, hours__isnull=False)
                ),
                name="leave_transaction_exactly_one_of_days_or_hours",
            ),
            # THE sign convention, as data the database itself enforces rather
            # than a rule only the service layer remembers. Every branch below
            # guards with an explicit `__isnull=False` before comparing sign —
            # PostgreSQL treats a CHECK expression that evaluates to NULL as
            # SATISFIED, not violated, so `hours__gt=0` alone on a row whose
            # `hours` IS NULL would evaluate NULL rather than FALSE, and OR
            # against another FALSE branch would leave the whole CHECK NULL —
            # passing a wrong-signed row through silently. The `isnull=False`
            # guard makes that branch resolve to a definite FALSE instead,
            # exactly as the model docstring explains.
            models.CheckConstraint(
                condition=(
                    models.Q(transaction_type="accrual", days__isnull=False, days__gt=0)
                    | models.Q(transaction_type="accrual", hours__isnull=False, hours__gt=0)
                    | models.Q(transaction_type="opening_balance", days__isnull=False, days__gt=0)
                    | models.Q(transaction_type="opening_balance", hours__isnull=False, hours__gt=0)
                    | models.Q(transaction_type="taken", days__isnull=False, days__lt=0)
                    | models.Q(transaction_type="taken", hours__isnull=False, hours__lt=0)
                    | models.Q(transaction_type="payout", days__isnull=False, days__lt=0)
                    | models.Q(transaction_type="payout", hours__isnull=False, hours__lt=0)
                    | models.Q(transaction_type="forfeiture", days__isnull=False, days__lt=0)
                    | models.Q(transaction_type="forfeiture", hours__isnull=False, hours__lt=0)
                    | models.Q(transaction_type="adjustment")
                    | models.Q(transaction_type="reversal")
                ),
                name="leave_transaction_sign_matches_type",
            ),
            models.CheckConstraint(
                condition=~models.Q(transaction_type="adjustment") | ~models.Q(reason=""),
                name="leave_transaction_adjustment_states_a_reason",
            ),
            # A reversal names what it reverses; nothing else may.
            models.CheckConstraint(
                condition=models.Q(transaction_type="reversal", reverses_transaction__isnull=False)
                | (
                    ~models.Q(transaction_type="reversal")
                    & models.Q(reverses_transaction__isnull=True)
                ),
                name="leave_transaction_reversal_names_what_it_reverses",
            ),
        ]

    def __str__(self):
        value = self.days if self.days is not None else self.hours
        return f"{self.employee_id} {self.leave_type_id} {self.transaction_type} {value}"

    def clean(self):
        super().clean()

        # A second, independent guard alongside the CHECK above: the CHECK
        # proves exactly one of days/hours is set and correctly signed; it
        # cannot see leave_cycle.unit, since a CHECK is one row, one table.
        # This is what stops a caller posting an hours transaction against a
        # days-unit cycle (or the reverse) — a mismatch the CHECK alone
        # cannot catch, and exactly the kind of silent unit confusion D-164
        # exists to prevent.
        if self.leave_cycle_id is not None:
            cycle_unit = self.leave_cycle.unit
            if cycle_unit == LeaveCycle.Unit.DAYS and self.hours is not None:
                raise ValidationError(
                    {
                        "hours": (
                            "This cycle is denominated in DAYS, but this transaction "
                            "carries an HOURS figure. Post it as days, or check that "
                            "this is really the right cycle."
                        )
                    }
                )
            if cycle_unit == LeaveCycle.Unit.HOURS and self.days is not None:
                raise ValidationError(
                    {
                        "days": (
                            "This cycle is denominated in HOURS, but this transaction "
                            "carries a DAYS figure. Post it as hours, or check that "
                            "this is really the right cycle."
                        )
                    }
                )


class LeaveAccrualRun(AuditedModel, TenantScopedModel):
    """One monthly accrual pass, for one employer, leave type and as-at date.

    ``UNIQUE (employer, leave_type, accrual_as_at)`` is what makes a second
    run write nothing (task 4) — a database constraint, not merely a status
    check the engine remembers to make, so even a retried task cannot post
    twice. ``leave/accrual.py::run_monthly_accrual()`` also checks for an
    existing COMPLETED run first and returns it unchanged, so the ordinary
    case never even reaches the constraint.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    employer = models.ForeignKey(
        "employers.Employer", on_delete=models.PROTECT, related_name="leave_accrual_runs"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="accrual_runs")
    accrual_as_at = models.DateField(db_index=True, help_text="Usually month end.")

    employees_processed = models.IntegerField(default=0)
    transactions_created = models.IntegerField(default=0)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    error_detail = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "leave_accrual_run"
        ordering = ["-accrual_as_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["employer", "leave_type", "accrual_as_at"],
                name="uniq_leave_accrual_run_per_employer_type_date",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "running", "completed", "failed"]),
                name="leave_accrual_run_status_is_known",
            ),
        ]

    def __str__(self):
        return f"Leave accrual {self.employer_id} {self.leave_type_id} as at {self.accrual_as_at}"


class LeaveApplication(AuditedModel, TenantScopedModel):
    """One employee's request for leave, over a span of dates (P6 chunk 2, task 2).

    **Status transitions are enforced in ``leave/applications.py`` and
    ``leave/authorisation.py`` as well as by the CHECK below** — the CHECK
    proves a value is one of the six known ones, never that a given MOVE
    between them was legal (the same distinction D-145 already draws for
    ``employee_import_batch``). ``draft -> submitted -> approved -> taken``
    and ``... -> declined`` / ``... -> cancelled`` are the only paths the
    service layer allows.

    **``reference`` is per tenant, human-facing, and never reused.**
    ``LV-{year}-{sequence}`` — see ``leave/applications.py::_next_reference()``.

    **No overlapping APPROVED applications, per employee** — a partial
    EXCLUDE, restricted to ``status='approved'`` by its own ``condition``
    (PostgreSQL exclusion constraints support a ``WHERE`` predicate exactly
    like a partial index): a draft or a declined application may legitimately
    share dates with another, but two approved ones covering the same day
    would double-book the same leave. Both ends of ``[start_date,
    end_date]`` are INCLUSIVE here, unlike ``leave_cycle``'s half-open
    convention — an application "from Monday to Friday" means five days
    including Friday, not four.

    **An overdrawn application is never refused** (task 2, Kobus's own
    framing). Approving one sets ``exceeds_balance`` and ``unpaid_days``
    rather than declining outright — see ``leave/authorisation.py::approve()``
    for exactly how the excess is capped at the ledger rather than let it
    run the balance negative.

    **Self-approval is visible, never hidden** (task 3). ``self_approved``
    is TRUE only where the owner is also the applicant and no other approver
    exists in the chain; the CHECK below requires ``self_approval_reason``
    whenever it is, and the leave register reads both columns directly —
    there is no separate "hide this" flag.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"
        TAKEN = "taken", "Taken"

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="leave_applications"
    )
    reference = models.CharField(max_length=20, help_text="Human reference, e.g. LV-2026-00042.")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="applications")
    leave_evidence_type = models.ForeignKey(
        LeaveEvidenceType,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="applications",
        help_text="Which of the four sick-leave variants, if any.",
    )

    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    total_days = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        default=0,
        help_text="Working days only, from the work schedule.",
    )
    total_hours = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    is_part_day = models.BooleanField(default=False)
    reason = models.CharField(max_length=500, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    submitted_by_user = models.ForeignKey(
        "core.AppUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by_user = models.ForeignKey(
        "core.AppUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
        help_text="The employer authorisation.",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.CharField(max_length=500, blank=True)
    evidence_file = models.ForeignKey(
        "core.FileObject",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Medical certificate.",
    )

    balance_at_submission = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Snapshot for the audit trail.",
    )
    exceeds_balance = models.BooleanField(
        default=False, help_text="Overdrawn days fall to unpaid unless overridden."
    )
    unpaid_days = models.DecimalField(
        max_digits=7, decimal_places=3, default=0, help_text="Portion that will be unpaid."
    )
    cancelled_reason = models.CharField(max_length=255, blank=True)

    self_approved = models.BooleanField(
        default=False,
        help_text="TRUE only where the owner is also the applicant and no other approver exists.",
    )
    self_approval_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Mandatory when self_approved — visible in the leave register, never hidden.",
    )

    class Meta:
        db_table = "leave_application"
        ordering = ["-start_date", "-id"]
        indexes = [
            models.Index(fields=["employee", "start_date"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["start_date", "end_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "reference"], name="uniq_leave_application_reference"
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="leave_application_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["draft", "submitted", "approved", "declined", "cancelled", "taken"]
                ),
                name="leave_application_status_is_known",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="approved") | models.Q(decided_by_user__isnull=False),
                name="leave_application_approval_names_the_approver",
            ),
            models.CheckConstraint(
                condition=~models.Q(self_approved=True) | ~models.Q(self_approval_reason=""),
                name="leave_application_self_approval_states_a_reason",
            ),
            # Partial EXCLUDE: only rows that are actually approved compete for a
            # date. A draft or a declined application legitimately shares dates
            # with another — only two APPROVED ones double-book the same leave.
            ExclusionConstraint(
                name="leave_application_no_overlapping_approved",
                expressions=[
                    (
                        DateRange(
                            "start_date",
                            "end_date",
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                    ("employee", RangeOperators.EQUAL),
                ],
                condition=models.Q(status="approved"),
            ),
        ]

    def __str__(self):
        return f"{self.reference} {self.employee_id} {self.status}"


class LeaveApplicationDay(AuditedModel, TenantScopedModel):
    """One calendar date inside a ``leave_application``'s span (task 2).

    A row exists for EVERY calendar date from ``start_date`` to ``end_date``
    inclusive — including a rest day or a public holiday inside the span —
    because the audit needs to show WHY a day inside a five-day application
    only deducted four: ``is_working_day`` is FALSE for both, and
    ``deducted_from_balance`` stays FALSE alongside it. A week's leave over
    a public holiday costs four days, not five; getting this backwards takes
    a day of leave the employee keeps under the Act.

    **``day_portion`` is the salaried-basis figure** — 1.000 for a full
    working day, 0.500 for a half day, the minimum increment for a salaried
    basis. **``hours`` is the hourly-accrual figure** — populated only when
    the employee's own accrual method is per-hours-worked, carrying that
    day's scheduled hours (or half of them for a part day) rather than a
    day-equivalent conversion, per D-164: nothing here converts one to the
    other. A salaried employee's ``hours`` stays NULL; an hourly employee's
    ``day_portion`` is still set (1.000/0.500, describing the SHAPE of the
    day — a whole day off or a half day off) but is not itself what is
    deducted from their HOURS balance.

    **``is_paid`` is FALSE where the balance is exhausted or the evidence
    rule withholds pay** (task 2) — never where the leave itself was
    refused, because it never is. **``deducted_from_balance`` is the audit
    of what actually left the ledger**: TRUE for a working day the balance
    could actually cover, FALSE for a rest day, a public holiday, or the
    portion of an overdrawn application beyond what the ledger held.
    """

    leave_application = models.ForeignKey(
        LeaveApplication, on_delete=models.CASCADE, related_name="days"
    )
    leave_date = models.DateField(db_index=True)
    day_portion = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=1,
        help_text="1.000 full day, 0.500 half day.",
    )
    hours = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    is_working_day = models.BooleanField(
        default=True, help_text="FALSE for rest days and public holidays — not deducted."
    )
    is_public_holiday = models.BooleanField(default=False)
    is_paid = models.BooleanField(
        default=True, help_text="FALSE where the balance is exhausted or evidence withholds pay."
    )
    deducted_from_balance = models.BooleanField(default=True)

    class Meta:
        db_table = "leave_application_day"
        ordering = ["leave_application_id", "leave_date"]
        indexes = [models.Index(fields=["tenant", "leave_date"])]
        constraints = [
            models.UniqueConstraint(
                fields=["leave_application", "leave_date"],
                name="uniq_leave_application_day_per_application",
            ),
            models.CheckConstraint(
                condition=models.Q(day_portion__gt=0, day_portion__lte=1),
                name="leave_application_day_portion_in_range",
            ),
        ]

    def __str__(self):
        return f"{self.leave_application_id} {self.leave_date}"
