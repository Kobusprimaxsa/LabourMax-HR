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
from core.models import TenantScopedModel, TenantSharedModel
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

    **Unit, not just a signed number** (D-C). ``quantity`` is signed and
    counted in ``unit`` — days or hours, whichever the accrual that produced
    THIS employee's balance for THIS leave type actually produced. Nothing
    in this codebase converts between them here; that reading needs the
    employee's own work schedule, is lossy, and is P7's problem at payout.

    Two placeholders follow the house pattern used everywhere a table this
    one points at does not exist yet (``core.TenantMembership.employee_id_ref``,
    ``attendance_day``'s two): ``leave_application_id_ref`` becomes a real FK
    to ``leave_application`` in chunk 2; ``payroll_run_id_ref`` becomes a real
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

    class Unit(models.TextChoices):
        DAYS = "days", "Days"
        HOURS = "hours", "Hours"

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
    quantity = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        help_text="Signed. See the model docstring's sign convention.",
    )
    unit = models.CharField(max_length=10, choices=Unit.choices)

    leave_application_id_ref = models.BigIntegerField(
        null=True, blank=True, help_text="Becomes a real FK to leave_application in chunk 2."
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
            models.CheckConstraint(
                condition=models.Q(unit__in=["days", "hours"]),
                name="leave_transaction_unit_is_known",
            ),
            # THE sign convention, as data the database itself enforces rather
            # than a rule only the service layer remembers.
            models.CheckConstraint(
                condition=(
                    models.Q(transaction_type="accrual", quantity__gt=0)
                    | models.Q(transaction_type="opening_balance", quantity__gt=0)
                    | models.Q(transaction_type="taken", quantity__lt=0)
                    | models.Q(transaction_type="payout", quantity__lt=0)
                    | models.Q(transaction_type="forfeiture", quantity__lt=0)
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
        return f"{self.employee_id} {self.leave_type_id} {self.transaction_type} {self.quantity}"


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
