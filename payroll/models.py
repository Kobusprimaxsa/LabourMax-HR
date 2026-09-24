"""Payroll periods — phase P3's half of domain 8.

``pay_period`` is generated, never typed. An employer sets the calendar once on the
pay group and a year of periods falls out of it, which is P3's definition of done.

Two columns carry more weight than they look:

``payment_date``
    Decides which tax year the earnings fall into. Not the period end, and not the
    period start — the date the money moves. A weekly period ending 27 February that
    pays on 3 March belongs to the NEW tax year, and getting that backwards moves a
    week of earnings onto the wrong IRP5.

``status``
    ``open`` until a run touches it, ``closed`` when the run finalises. ``reopened``
    and ``reopened_count`` exist because reopening a closed period is a real thing
    that happens and a thing somebody must later account for — so it is counted
    rather than hidden.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models.functions import Round

from core.audit import AuditedModel
from core.models import TenantScopedModel


class PayPeriod(AuditedModel, TenantScopedModel):
    """One pay period for one pay group, inside one tax year."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        CLOSED = "closed", "Closed"
        REOPENED = "reopened", "Reopened"

    pay_group = models.ForeignKey(
        "employers.PayGroup", on_delete=models.PROTECT, related_name="pay_periods"
    )
    tax_year = models.ForeignKey(
        "statutory.TaxYear",
        on_delete=models.PROTECT,
        related_name="pay_periods",
        help_text="The year the PAYMENT DATE falls into, not the period end.",
    )
    period_number = models.SmallIntegerField(
        help_text="1..12 monthly, 1..52 weekly, within the tax year."
    )

    period_start = models.DateField(db_index=True)
    period_end = models.DateField(db_index=True)
    payment_date = models.DateField(
        db_index=True, help_text="Determines which tax year the earnings fall into."
    )

    working_days_in_period = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=0,
        help_text=(
            "Days in the period matching the employer's working pattern. Public "
            "holidays are NOT subtracted: one falling on an ordinary working day is "
            "paid, so it is an available day for pay purposes."
        ),
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    reopened_count = models.SmallIntegerField(
        default=0, help_text="Every reopen is an audited event."
    )

    class Meta:
        db_table = "pay_period"
        ordering = ["pay_group_id", "period_start"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["payment_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["pay_group", "period_start"], name="uniq_period_start_per_pay_group"
            ),
            models.UniqueConstraint(
                fields=["pay_group", "tax_year", "period_number"],
                name="uniq_period_number_per_group_year",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="pay_period_end_not_before_start",
            ),
            models.CheckConstraint(
                condition=models.Q(payment_date__gte=models.F("period_start")),
                name="pay_period_payment_not_before_start",
            ),
            models.CheckConstraint(
                condition=models.Q(reopened_count__gte=0),
                name="pay_period_reopened_count_not_negative",
            ),
            # A closed period has a closing timestamp, and an open one does not. The
            # date is the record; the status alone loses when it happened.
            models.CheckConstraint(
                condition=models.Q(status="closed", closed_at__isnull=False)
                | ~models.Q(status="closed"),
                name="pay_period_closed_has_a_timestamp",
            ),
        ]

    def __str__(self):
        return (
            f"{self.pay_group_id} period {self.period_number}: "
            f"{self.period_start} to {self.period_end}"
        )


class PayrollCalculationTrace(AuditedModel, TenantScopedModel):
    """What a calculator did, stored: invariant 5 (D-208).

    "When an employee disputes a figure from eighteen months ago, the answer is a
    stored record — not a re-run of today's code against today's rates." So this
    keeps the INPUTS it was given, the PRIMARY KEYS of every statutory row it
    read, the OUTPUTS it produced and any WARNINGS, per payslip per calculator.

    **The calculator produces the structure; this row persists it.** The pure
    function returns a ``calculators.base.CalculationTrace`` and knows nothing
    about a database; the caller writes it here. That boundary is why the same
    calculation can be run in a test, in a payroll run, or replayed in a dispute.

    ``statutory_rows`` holds ``[["statutory_parameter", 901], ...]`` — keys, not
    citation text. A citation can be corrected later (two have been in this
    build); the key still opens the row the payslip was actually computed
    against.

    **Written even for a zero, and even when the calculator warned.** A missing
    trace row means "this never ran", and it must not be able to mean anything
    else.

    **Append-only, like a ledger row.** A trace that can be edited after the fact
    is not evidence of anything. The trigger, not a convention, is what holds
    that (``core/db/rls.py::append_only()``).

    ``payslip`` was a forward-reference BigIntegerField until the payslip table
    was built, and is a real foreign key now (D-208 said it would become one in
    that chunk). It stays NULLABLE: a calculation run outside a payslip — a
    what-if on a screen, or any of these tests — still writes its evidence.
    """

    payslip = models.ForeignKey(
        "payroll.Payslip",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calculation_traces",
        help_text="Null while a calculation is run outside a payslip, as every test does.",
    )
    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="calculation_traces"
    )
    calculator = models.CharField(
        max_length=60, db_index=True, help_text="e.g. 'uif.contribution'."
    )
    calculated_for = models.DateField(
        db_index=True, help_text="The date the calculation is FOR, never the date it ran."
    )

    inputs = models.JSONField(
        help_text="Every input, as given. Strings, so it reads the same in 2029."
    )
    statutory_rows = models.JSONField(
        default=list, help_text='[["table", row_id], ...] - keys, never citation text.'
    )
    outputs = models.JSONField(help_text="Every figure produced, unrounded.")
    warnings = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "payroll_calculation_trace"
        ordering = ["-calculated_for", "-id"]
        indexes = [
            models.Index(fields=["tenant", "calculator", "calculated_for"]),
            models.Index(fields=["employee", "calculated_for"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(calculator=""),
                name="calculation_trace_names_its_calculator",
            ),
        ]

    def __str__(self):
        return f"{self.calculator} for {self.employee_id} on {self.calculated_for}"


class PayrollRun(AuditedModel, TenantScopedModel):
    """One run of one pay period. The TABLE; the state machine is not built.

    P7's assembly — generating payslips, calling the calculators, finalising —
    is blocked on P2 verification: ``in_force_on()`` cannot see unverified
    reference data, so nothing can compute. The table is not blocked by that and
    the payslip needs something to belong to, so it exists here with its
    statuses, its timestamps and its reversal link, and with no orchestration.

    **The CHECK proves a status is a KNOWN one; it never proves the move from
    the previous value was legal** (D-145, learned on the import batch). The
    transition function is assembly and is not here — so nothing in this chunk
    may be read as "runs are safe to drive by hand".

    A reversal is a run of its own pointing at what it reverses (invariant 4).
    Finalised payslips are never edited; the correction is a reversing payslip
    plus a replacement in a new run, and this is the row that ties the two.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CALCULATING = "calculating", "Calculating"
        CALCULATED = "calculated", "Calculated"
        APPROVED = "approved", "Approved"
        FINALISED = "finalised", "Finalised"
        REVERSED = "reversed", "Reversed"

    pay_period = models.ForeignKey(PayPeriod, on_delete=models.PROTECT, related_name="runs")
    run_number = models.SmallIntegerField(
        default=1,
        help_text="1 for the ordinary run; 2+ for a correction run over the same period.",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    calculated_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_payroll_runs",
    )
    finalised_at = models.DateTimeField(null=True, blank=True)
    finalised_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="finalised_payroll_runs",
    )

    reverses_run = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        help_text="Set on a reversal run, naming the run it undoes.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "payroll_run"
        ordering = ["pay_period_id", "run_number"]
        indexes = [models.Index(fields=["tenant", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["pay_period", "run_number"], name="uniq_run_number_per_period"
            ),
            models.CheckConstraint(
                condition=models.Q(run_number__gte=1), name="payroll_run_number_is_positive"
            ),
            # Each terminal status carries the timestamp that says WHEN, because a
            # status alone loses that and an audit eighteen months later needs it.
            # Guarded both ways on the nullable column (the NULL-is-permissive trap):
            # a finalised row must have the timestamp, and a row that is not
            # finalised must not carry one.
            models.CheckConstraint(
                condition=~models.Q(status="approved") | models.Q(approved_at__isnull=False),
                name="payroll_run_approved_has_a_timestamp",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="finalised") | models.Q(finalised_at__isnull=False),
                name="payroll_run_finalised_has_a_timestamp",
            ),
            models.CheckConstraint(
                condition=models.Q(finalised_at__isnull=True)
                | models.Q(finalised_by_user__isnull=False),
                name="payroll_run_finalised_names_who",
            ),
        ]

    def __str__(self):
        return f"Run {self.run_number} of period {self.pay_period_id} ({self.status})"


class Payslip(AuditedModel, TenantScopedModel):
    """One employee, one run. Frozen the moment it is finalised.

    **Invariant 7: a payslip freezes what it showed.** ``employee_snapshot``
    holds the name, employee number, position, rate and bank reference AS AT
    finalisation, because reprinting a 2027 payslip after the employee married
    and changed banks must reproduce the ORIGINAL. Reading those through the
    foreign key at print time would reproduce today instead, and the reprint
    would differ from the document the employee was handed — which is the whole
    failure the snapshot exists to prevent.

    **Invariant 4: once finalised it is never updated or deleted.** Held by a
    trigger (``core/db/rls.py::no_change_when_finalised()``), not by a
    convention, and not only by the service that writes it — a REVOKE does not
    bind the table owner and the application connects as the owner. An
    unfinalised payslip is edited freely: the run is still being worked.

    The totals are the SUM OF THE ROUNDED LINES, at two decimal places.
    Invariant 6 puts rounding at the payslip line and nowhere else, so a total
    is what the lines add up to and not a separately rounded figure — those two
    differ by a cent often enough that an employee notices.
    """

    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="payslips")
    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="payslips"
    )

    #: Invariant 7. Written at finalisation and never read through the FK after.
    employee_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Name, number, position, rate and bank reference as at finalisation.",
    )

    gross_earnings = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    employer_contributions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    is_finalised = models.BooleanField(default=False, db_index=True)
    finalised_at = models.DateTimeField(null=True, blank=True)

    is_reversal = models.BooleanField(
        default=False, help_text="A reversing payslip. Its lines are the negation of the original."
    )
    reverses_payslip = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversed_by"
    )

    class Meta:
        db_table = "payslip"
        ordering = ["payroll_run_id", "employee_id"]
        indexes = [
            models.Index(fields=["tenant", "is_finalised"]),
            models.Index(fields=["employee", "-finalised_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll_run", "employee"], name="uniq_payslip_per_employee_per_run"
            ),
            # Both ways over the nullable timestamp, because a CHECK that
            # evaluates to NULL counts as satisfied (D-170).
            models.CheckConstraint(
                condition=models.Q(is_finalised=False, finalised_at__isnull=True)
                | models.Q(is_finalised=True, finalised_at__isnull=False),
                name="payslip_finalised_has_a_timestamp",
            ),
            models.CheckConstraint(
                condition=models.Q(is_reversal=True) | models.Q(reverses_payslip__isnull=True),
                name="payslip_only_a_reversal_reverses_something",
            ),
            models.CheckConstraint(
                condition=models.Q(is_reversal=False) | models.Q(reverses_payslip__isnull=False),
                name="payslip_a_reversal_names_what_it_reverses",
            ),
        ]

    def __str__(self):
        return f"Payslip for {self.employee_id} on run {self.payroll_run_id}"


class PayslipLine(AuditedModel, TenantScopedModel):
    """One line of one payslip, and the only place a figure is rounded.

    **The component code and the SARS source code are FROZEN as text**, beside
    the foreign keys rather than instead of them. Invariant 7 again: a source
    code row can be corrected and a component renamed, and a reprint must show
    what the payslip showed. The FK answers "which catalogue row is this"; the
    frozen text answers "what did this line say", and only the second survives a
    correction to the catalogue.

    ``amount`` is ``amount_exact`` rounded to two places, ROUND_HALF_UP, and a
    CHECK proves it rather than trusting the writer — invariant 6 made
    structural. PostgreSQL's ``round()`` on numeric rounds half away from zero,
    which is what ``ROUND_HALF_UP`` means in Python's decimal module, so the two
    agree on a negative line as well as a positive one.

    CASCADE from the payslip, which is the one place in this schema a cascade is
    right: a line has no existence apart from its payslip. The payslip itself is
    PROTECTed and, once finalised, cannot be deleted at all.
    """

    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="lines")
    payroll_component = models.ForeignKey(
        "employers.PayrollComponent", on_delete=models.PROTECT, related_name="payslip_lines"
    )

    #: Frozen copies. See the class docstring — these are what a reprint shows.
    component_code = models.CharField(max_length=40)
    source_code = models.CharField(
        max_length=10, blank=True, help_text="The SARS code as it stood, for the IRP5."
    )
    description = models.CharField(max_length=160)

    sequence = models.SmallIntegerField(default=0, help_text="Display order on the document.")

    units = models.DecimalField(
        max_digits=12, decimal_places=4, default=0, help_text="Hours, days or periods."
    )
    rate = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    #: Invariant 6: the unrounded figure is stored alongside the rounded one.
    amount_exact = models.DecimalField(max_digits=16, decimal_places=6)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        db_table = "payslip_line"
        ordering = ["payslip_id", "sequence", "id"]
        indexes = [
            models.Index(fields=["tenant", "component_code"]),
            models.Index(fields=["payslip", "sequence"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(component_code=""),
                name="payslip_line_names_its_component",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount=Round(models.F("amount_exact"), 2),
                ),
                name="payslip_line_amount_is_the_exact_figure_rounded",
            ),
        ]

    def __str__(self):
        return f"{self.component_code} {self.amount} on payslip {self.payslip_id}"


class YtdAccumulator(AuditedModel, TenantScopedModel):
    """Year-to-date totals per employee per tax year per SARS source code.

    **A CACHE and nothing more** (invariant 3). Every figure here is derivable
    by summing the employee's FINALISED payslip lines for the tax year, and
    ``payroll/ytd.py`` rebuilds it from exactly that — from scratch, never
    incrementally. P5 settled why (D-153): an incrementally maintained cache
    that has drifted cannot be told apart from a correct one, so there is no
    incremental path to drift.

    Keyed on the SOURCE CODE rather than the component, because what a
    year-to-date figure is FOR is the IRP5 and the EMP201, and both are stated
    in source codes. Two components sharing a code (BASIC, SUNDAY_2_0 and
    PH_WORKED are all 3601) belong on one line there, and keying on the
    component would split them.
    """

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="ytd_accumulators"
    )
    tax_year = models.ForeignKey(
        "statutory.TaxYear", on_delete=models.PROTECT, related_name="ytd_accumulators"
    )
    source_code = models.CharField(max_length=10, db_index=True)

    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    units = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    payslip_count = models.IntegerField(default=0)

    rebuilt_at = models.DateTimeField(
        help_text="When this row was last recomputed from finalised payslips."
    )

    class Meta:
        db_table = "ytd_accumulator"
        ordering = ["employee_id", "tax_year_id", "source_code"]
        indexes = [models.Index(fields=["tenant", "tax_year"])]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "tax_year", "source_code"],
                name="uniq_ytd_per_employee_year_code",
            ),
            models.CheckConstraint(
                condition=models.Q(payslip_count__gte=0),
                name="ytd_payslip_count_not_negative",
            ),
        ]

    def __str__(self):
        return f"YTD {self.source_code} for {self.employee_id}: {self.amount}"


class AnnualBonusCycle(AuditedModel, TenantScopedModel):
    """The statutory annual bonus as it accrues through its cycle (D-286).

    Sheet 02's ``annual_bonus_cycle``: "Tracks the contract-cleaning statutory
    annual bonus (4.333 weeks) as it accrues through the year, so a mid-year
    termination pro-rata is a lookup rather than a reconstruction."

    **A CACHE, like ``ytd_accumulator``** (invariant 3). ``full_months_worked``
    and the accrued amount are recomputed from scratch by
    ``payroll/bonus.py::accrue()`` — from the engagement, the remuneration
    history and the rule set in force — and never adjusted in place. A row
    whose status has moved past ``accruing`` is a paid record and is not
    rebuilt.

    **No row at all where the instrument gives no bonus.** The domestic sector
    and the BCEA carry no payment month, and a row of nil would read as a bonus
    that happened to be zero, which is a different fact.

    Two columns beyond sheet 02, both invariant-driven and flagged in D-286:
    ``accrued_amount_exact`` (invariant 6 — the unrounded figure stored beside
    the rounded one, held together by a CHECK exactly as ``payslip_line`` is)
    and ``accrued_as_at``, without which ``full_months_worked`` does not say
    as at WHEN.
    """

    class Status(models.TextChoices):
        ACCRUING = "accruing", "Accruing"
        PAID = "paid", "Paid"
        PRO_RATA_PAID = "pro_rata_paid", "Pro rata paid on termination"
        FORFEITED = "forfeited", "Forfeited"

    #: Statuses that are a payment, and so must name the run that paid them.
    PAID_STATUSES = (Status.PAID, Status.PRO_RATA_PAID)

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="annual_bonus_cycles"
    )
    cycle_start = models.DateField(db_index=True)
    cycle_end = models.DateField()
    bonus_weeks = models.DecimalField(
        max_digits=6, decimal_places=3, help_text="From termination_rule_set, as in force."
    )
    full_months_worked = models.SmallIntegerField(default=0)
    accrued_amount_exact = models.DecimalField(max_digits=16, decimal_places=6, default=0)
    accrued_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    accrued_as_at = models.DateField(
        help_text="The date full_months_worked and the accrued amount were computed as at."
    )
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_in_payroll_run = models.ForeignKey(
        PayrollRun,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="annual_bonus_cycles_paid",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACCRUING, db_index=True
    )

    class Meta:
        db_table = "annual_bonus_cycle"
        ordering = ["employee_id", "cycle_start"]
        indexes = [models.Index(fields=["tenant", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "cycle_start"], name="uniq_bonus_cycle_per_employee_start"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["accruing", "paid", "pro_rata_paid", "forfeited"]),
                name="bonus_cycle_status_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_end__gte=models.F("cycle_start")),
                name="bonus_cycle_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=models.Q(full_months_worked__gte=0, full_months_worked__lte=12),
                name="bonus_cycle_months_within_a_year",
            ),
            models.CheckConstraint(
                condition=models.Q(accrued_amount=Round(models.F("accrued_amount_exact"), 2)),
                name="bonus_cycle_amount_is_the_exact_amount_rounded",
            ),
            models.CheckConstraint(
                condition=models.Q(paid_amount__gte=0),
                name="bonus_cycle_paid_not_negative",
            ),
            # Both arms say what NULL means, explicitly (CLAUDE.md: every
            # constraint over a nullable column is permissive by default).
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["accruing", "forfeited"], paid_in_payroll_run__isnull=True
                )
                | models.Q(status__in=["paid", "pro_rata_paid"], paid_in_payroll_run__isnull=False),
                name="bonus_cycle_a_payment_names_its_run",
            ),
        ]

    def __str__(self):
        return f"Bonus cycle {self.cycle_start} for {self.employee_id}: {self.accrued_amount}"


class PayrollValidationIssue(AuditedModel, TenantScopedModel):
    """What a run must answer for before anyone is paid.

    Every issue belongs to a run, and most name an employee. ``severity``
    decides whether it merely warns or actually stops the run: a BLOCKING issue
    standing unresolved makes ``payroll/runs.py::approve()`` refuse outright.

    **Issues are DERIVED, not accumulated.** ``validate()`` deletes the run's
    unresolved issues and rewrites them from scratch every time it is called, in
    the same shape as ``ytd_accumulator`` (D-231) and ``timesheet_summary``
    before it (D-153): a list maintained incrementally that has drifted cannot
    be told apart from a correct one. An issue somebody RESOLVED survives, with
    its reason, because that resolution is a human decision and a record of one.

    **A resolved BLOCKING issue no longer blocks**, which is the whole point of
    being able to resolve one — an employer who has looked at a below-minimum
    wage and accepted it (D-108's own shape) must be able to proceed, and their
    name and reason are on the row for ever.
    """

    class Severity(models.TextChoices):
        BLOCKING = "blocking", "Blocking"
        WARNING = "warning", "Warning"

    payroll_run = models.ForeignKey(
        "payroll.PayrollRun", on_delete=models.CASCADE, related_name="validation_issues"
    )
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_validation_issues",
        help_text="Null for an issue about the run or the period rather than a person.",
    )

    code = models.CharField(
        max_length=60, db_index=True, help_text="A stable identifier, e.g. 'reference_data'."
    )
    severity = models.CharField(max_length=20, choices=Severity.choices, db_index=True)
    message = models.TextField(help_text="What is wrong, in the terms the employer must act on.")

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="resolved_payroll_issues",
    )
    resolution_reason = models.TextField(blank=True)

    class Meta:
        db_table = "payroll_validation_issue"
        ordering = ["payroll_run_id", "severity", "employee_id", "code"]
        indexes = [
            models.Index(fields=["tenant", "severity"]),
            models.Index(fields=["payroll_run", "resolved_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(code=""), name="validation_issue_has_a_code"
            ),
            models.CheckConstraint(
                condition=~models.Q(message=""), name="validation_issue_says_what_is_wrong"
            ),
            # Resolving is a human act with a name and a reason on it. All three
            # together or none of them — and guarded both ways over the nullable
            # columns, because a CHECK that evaluates to NULL counts as satisfied.
            models.CheckConstraint(
                condition=models.Q(
                    resolved_at__isnull=True,
                    resolved_by_user__isnull=True,
                    resolution_reason="",
                )
                | models.Q(
                    resolved_at__isnull=False,
                    resolved_by_user__isnull=False,
                )
                & ~models.Q(resolution_reason=""),
                name="validation_issue_resolution_is_named_and_reasoned",
            ),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.code} on run {self.payroll_run_id}"
