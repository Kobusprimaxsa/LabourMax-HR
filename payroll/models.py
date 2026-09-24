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

    ``reference_rows_used`` holds ``[["statutory_parameter", 901], ...]`` — keys, not
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
        # CASCADE, and the trigger decides whether it may: a DRAFT payslip's
        # evidence goes with the draft when a run is recalculated; a FINALISED
        # payslip cannot be deleted at all, so its traces never go (D-294).
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="calculation_traces",
        help_text="Null while a calculation is run outside a payslip, as every test does.",
    )
    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="calculation_traces"
    )
    calculator_name = models.CharField(
        max_length=60, db_index=True, help_text="e.g. 'uif.contribution'."
    )
    #: Execution order within one payslip (sheet 02). 0 outside a payslip.
    sequence = models.SmallIntegerField(default=0)
    calculated_for = models.DateField(
        db_index=True, help_text="The date the calculation is FOR, never the date it ran."
    )

    inputs = models.JSONField(
        help_text="Every input, as given. Strings, so it reads the same in 2029."
    )
    reference_rows_used = models.JSONField(
        default=list, help_text='[["table", row_id], ...] - keys, never citation text.'
    )
    outputs = models.JSONField(help_text="Every figure produced, unrounded.")
    warnings = models.JSONField(default=list, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "payroll_calculation_trace"
        ordering = ["-calculated_for", "-id"]
        indexes = [
            models.Index(fields=["tenant", "calculator_name", "calculated_for"]),
            models.Index(fields=["employee", "calculated_for"]),
            models.Index(fields=["payslip", "sequence"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(calculator_name=""),
                name="calculation_trace_names_its_calculator",
            ),
        ]

    def __str__(self):
        return f"{self.calculator_name} for {self.employee_id} on {self.calculated_for}"


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
        #: Sheet 02's. Reached only when calculation itself breaks — a refusal
        #: about ONE employee is a blocking issue on that employee, and the run
        #: still calculates everybody else (D-292).
        FAILED = "failed", "Failed"

    class RunType(models.TextChoices):
        REGULAR = "regular", "Regular"
        SUPPLEMENTARY = "supplementary", "Supplementary"
        TERMINATION = "termination", "Termination"
        BONUS = "bonus", "Bonus"
        CORRECTION = "correction", "Correction"

    employer = models.ForeignKey(
        "employers.Employer", on_delete=models.PROTECT, related_name="payroll_runs"
    )
    pay_period = models.ForeignKey(PayPeriod, on_delete=models.PROTECT, related_name="runs")
    run_type = models.CharField(
        max_length=20, choices=RunType.choices, default=RunType.REGULAR, db_index=True
    )
    run_number = models.SmallIntegerField(
        default=1,
        help_text="1 for the ordinary run; 2+ for a correction run over the same period.",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    employee_count = models.IntegerField(default=0)
    total_gross = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_paye = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_uif_employee = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_uif_employer = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_sdl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_other_deductions = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_net_pay = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_employer_cost = models.DecimalField(
        max_digits=16,
        decimal_places=2,
        default=0,
        help_text="Gross + UIF employer + SDL + COIDA provision.",
    )

    reference_data_version = models.ForeignKey(
        "statutory.ReferenceDataVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payroll_runs",
        help_text="The statutory data version the run was calculated against.",
    )
    engine_version = models.CharField(
        max_length=20,
        help_text="calculators.base.ENGINE_VERSION when calculated — needed to reproduce a run.",
    )

    calculated_at = models.DateTimeField(null=True, blank=True)
    calculated_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calculated_payroll_runs",
    )
    validation_summary = models.JSONField(
        default=list, blank=True, help_text="Blocking errors and non-blocking warnings."
    )
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
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["employer", "-finalised_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "draft",
                        "calculating",
                        "calculated",
                        "approved",
                        "finalised",
                        "reversed",
                        "failed",
                    ]
                ),
                name="payroll_run_status_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    run_type__in=["regular", "supplementary", "termination", "bonus", "correction"]
                ),
                name="payroll_run_type_is_known",
            ),
            models.CheckConstraint(
                condition=~models.Q(engine_version=""),
                name="payroll_run_names_its_engine_version",
            ),
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

    class PaymentMethod(models.TextChoices):
        EFT = "eft", "EFT"
        CASH = "cash", "Cash"

    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="payslips")
    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="payslips"
    )
    pay_period = models.ForeignKey(PayPeriod, on_delete=models.PROTECT, related_name="payslips")
    engagement = models.ForeignKey(
        "employees.EmployeeEngagement", on_delete=models.PROTECT, related_name="payslips"
    )
    payslip_number = models.CharField(max_length=30)

    #: Invariant 7. Written at finalisation and never read through the FK after.
    employee_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Name, number, position, rate and bank reference as at finalisation.",
    )

    pay_basis = models.CharField(max_length=20)
    rate_used = models.DecimalField(max_digits=14, decimal_places=6)
    ordinary_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    overtime_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    days_worked = models.DecimalField(max_digits=7, decimal_places=3, default=0)

    gross_remuneration = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    taxable_remuneration = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    uif_remuneration = models.DecimalField(
        max_digits=14, decimal_places=2, default=0, help_text="Base after the ceiling is applied."
    )
    sdl_remuneration = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paye = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    uif_employee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    uif_employer = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sdl_employer = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    total_earnings = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_employer_contributions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_pay = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.EFT
    )
    bank_account = models.ForeignKey(
        "employees.EmployeeBankAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payslips",
    )
    is_termination_payslip = models.BooleanField(default=False)

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
            models.Index(fields=["employee", "pay_period"]),
            models.Index(fields=["tenant", "pay_period"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll_run", "employee"], name="uniq_payslip_per_employee_per_run"
            ),
            models.UniqueConstraint(
                fields=["tenant", "payslip_number"], name="uniq_payslip_number_per_tenant"
            ),
            models.CheckConstraint(
                condition=~models.Q(payslip_number=""), name="payslip_has_a_number"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    net_pay=models.F("total_earnings") - models.F("total_deductions")
                ),
                name="payslip_net_is_earnings_less_deductions",
            ),
            # Sheet 03's "net_pay >= 0", EXCEPT on a reversal, whose lines are the
            # negation of the original's and whose net is therefore negative by
            # construction (invariant 4). A negative net on an ordinary payslip is
            # a blocking validation issue before it is ever a stored row.
            models.CheckConstraint(
                condition=models.Q(is_reversal=True) | models.Q(net_pay__gte=0),
                name="payslip_net_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(payment_method__in=["eft", "cash"]),
                name="payslip_payment_method_is_known",
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

    ``amount`` is ``amount_unrounded`` rounded to two places, ROUND_HALF_UP, and a
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

    class ComponentType(models.TextChoices):
        EARNING = "earning", "Earning"
        DEDUCTION = "deduction", "Deduction"
        EMPLOYER_CONTRIBUTION = "employer_contribution", "Employer contribution"
        INFORMATIONAL = "informational", "Informational"

    class UnitType(models.TextChoices):
        HOURS = "hours", "Hours"
        DAYS = "days", "Days"
        MONTHS = "months", "Months"
        NONE = "none", "None"

    line_order = models.SmallIntegerField(default=0, help_text="Display order on the document.")
    component_type = models.CharField(max_length=30, choices=ComponentType.choices, db_index=True)
    #: Frozen copies. See the class docstring — these are what a reprint shows.
    component_code = models.CharField(max_length=40)
    sars_source_code = models.ForeignKey(
        "statutory.SarsSourceCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payslip_lines",
    )
    source_code = models.CharField(
        max_length=10, blank=True, help_text="The SARS code as it stood, for the IRP5."
    )
    description = models.CharField(max_length=150)

    units = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, help_text="Hours or days."
    )
    unit_type = models.CharField(max_length=10, choices=UnitType.choices, blank=True)
    rate = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    multiplier = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="Rounded HALF_UP to 2dp at this level only."
    )
    amount_unrounded = models.DecimalField(
        max_digits=18, decimal_places=6, help_text="Retained so rounding can be reconciled."
    )

    #: The component's base flags AS THEY STOOD when the line was written —
    #: frozen, like the codes beside them (invariant 7).
    is_taxable = models.BooleanField(default=True)
    is_uif_base = models.BooleanField(default=True)
    is_sdl_base = models.BooleanField(default=True)
    calculation_note = models.CharField(
        max_length=255, blank=True, help_text="Plain-English explanation for the drill-down."
    )
    #: NOT in sheet 02 (D-302). The recurring line this payslip line priced, so a
    #: loan's balance is DERIVED — principal less its finalised lines — rather than
    #: a column decremented at finalisation that nothing could rebuild once it
    #: drifted (invariant 3). Two advances of one component run at once (D-135),
    #: so the component code alone cannot say which one a line paid down.
    recurring_component = models.ForeignKey(
        "employees.EmployeeRecurringComponent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payslip_lines",
    )

    class Meta:
        db_table = "payslip_line"
        ordering = ["payslip_id", "line_order", "id"]
        indexes = [
            models.Index(fields=["tenant", "component_code"]),
            models.Index(fields=["payslip", "line_order"]),
            models.Index(fields=["tenant", "payroll_component"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(component_code=""),
                name="payslip_line_names_its_component",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount=Round(models.F("amount_unrounded"), 2),
                ),
                name="payslip_line_amount_is_the_exact_figure_rounded",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    component_type__in=[
                        "earning",
                        "deduction",
                        "employer_contribution",
                        "informational",
                    ]
                ),
                name="payslip_line_component_type_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_type__in=["", "hours", "days", "months", "none"]),
                name="payslip_line_unit_type_is_known",
            ),
        ]

    def __str__(self):
        return f"{self.component_code} {self.amount} on payslip {self.payslip_id}"


class YtdAccumulator(AuditedModel, TenantScopedModel):
    """Year-to-date totals for one employee, one tax year, one employer — sheet
    02's shape (D-290, superseding D-231's one-row-per-source-code layout).

    **A CACHE and nothing more** (invariant 3), and D-231's real point survives
    the reshape: every figure is derivable from the employee's FINALISED
    payslips for the tax year, ``payroll/ytd.py`` rebuilds the row from exactly
    that, from scratch, and never increments it (D-153).

    The named totals are what the EMP201 and the payslip's YTD column read; the
    per-code figures the IRP5 needs are ``ytd_by_source_code`` —
    ``{"3601": "45000.00", ...}``, strings so a Decimal never passes through a
    float on its way into JSON. Three components share 3601 and land on one key.
    """

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="ytd_accumulators"
    )
    tax_year = models.ForeignKey(
        "statutory.TaxYear", on_delete=models.PROTECT, related_name="ytd_accumulators"
    )
    employer = models.ForeignKey(
        "employers.Employer", on_delete=models.PROTECT, related_name="ytd_accumulators"
    )

    periods_processed = models.SmallIntegerField(default=0)
    ytd_gross = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_taxable = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_paye = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_uif_employee = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_uif_employer = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_sdl = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_uif_remuneration = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    ytd_coida_remuneration = models.DecimalField(
        max_digits=16, decimal_places=2, default=0, help_text="Capped at the COIDA ceiling."
    )
    ytd_by_source_code = models.JSONField(
        default=dict, blank=True, help_text="{'3601': '45000.00', ...} — the IRP5 payload."
    )
    last_payroll_run = models.ForeignKey(
        PayrollRun,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ytd_accumulators",
    )
    recalculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ytd_accumulator"
        ordering = ["employee_id", "tax_year_id"]
        indexes = [models.Index(fields=["tenant", "tax_year"])]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "tax_year", "employer"],
                name="uniq_ytd_per_employee_year_employer",
            ),
            models.CheckConstraint(
                condition=models.Q(periods_processed__gte=0),
                name="ytd_periods_processed_not_negative",
            ),
        ]

    def __str__(self):
        return f"YTD {self.tax_year_id} for {self.employee_id}: {self.ytd_gross}"


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
        #: Sheet 02's "error". Named BLOCKING in code because that is what it
        #: does: an unacknowledged one stops approval.
        BLOCKING = "error", "Error — blocks approval"
        WARNING = "warning", "Warning"
        INFO = "info", "Information"

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

    issue_code = models.CharField(
        max_length=60, db_index=True, help_text="A stable identifier, e.g. 'reference_data'."
    )
    severity = models.CharField(max_length=20, choices=Severity.choices, db_index=True)
    message = models.TextField(help_text="What is wrong, in the terms the employer must act on.")
    detail = models.JSONField(default=dict, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="acknowledged_payroll_issues",
    )
    resolution_reason = models.TextField(blank=True)

    class Meta:
        db_table = "payroll_validation_issue"
        ordering = ["payroll_run_id", "severity", "employee_id", "issue_code"]
        indexes = [
            models.Index(fields=["tenant", "severity"]),
            models.Index(fields=["payroll_run", "acknowledged_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(issue_code=""), name="validation_issue_has_a_code"
            ),
            models.CheckConstraint(
                condition=~models.Q(message=""), name="validation_issue_says_what_is_wrong"
            ),
            # Resolving is a human act with a name and a reason on it. All three
            # together or none of them — and guarded both ways over the nullable
            # columns, because a CHECK that evaluates to NULL counts as satisfied.
            models.CheckConstraint(
                condition=models.Q(
                    acknowledged_at__isnull=True,
                    acknowledged_by_user__isnull=True,
                    resolution_reason="",
                )
                | models.Q(
                    acknowledged_at__isnull=False,
                    acknowledged_by_user__isnull=False,
                )
                & ~models.Q(resolution_reason=""),
                name="validation_issue_resolution_is_named_and_reasoned",
            ),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.issue_code} on run {self.payroll_run_id}"


class TerminationPayout(AuditedModel, TenantScopedModel):
    """The itemised final payment for one ended engagement — sheet 02's
    ``termination_payout``, column for column (P7 chunk 8c, D-308).

    Sheet 01: "Kept separate from the payslip so the working is visible and
    reviewable before the run." So it is PREPARED from the rows
    (``payroll/termination.py::prepare()``), REVIEWED by a person, and only then
    paid: the assembly refuses a leaver whose payout is not reviewed, and
    re-prices it and refuses again if the figure has moved since the review — a
    reviewed figure is what somebody agreed to, and a payslip may not quietly pay
    another. Finalising the run that paid it makes it ``processed`` and names the
    run; reversing that run puts it back to ``reviewed``.

    **A figure here is DERIVED and stored, never typed** (invariant 2's
    reasoning): every amount comes from ``calculators/termination.py``, whose
    trace is ``calculation_detail``, and ``total_payout_gross`` is held to the
    sum of its four parts by a CHECK.

    **``outstanding_deductions`` is always nil in this build.** Sheet 02 reads
    it as "loan balances recovered, within BCEA s34 limits"; recovering a whole
    loan balance from a final payment is a deduction the employee's written
    consent must cover in terms (s34(1)(a), "a debt specified in the
    agreement"), and nothing here can read what a consent file says. The loan's
    ordinary instalment still comes off the final payslip as a recurring line.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEWED = "reviewed", "Reviewed"
        PROCESSED = "processed", "Processed"
        CANCELLED = "cancelled", "Cancelled"

    class SeveranceTaxTreatment(models.TextChoices):
        STANDARD = "standard", "Standard"
        DIRECTIVE_REQUIRED = "directive_required", "SARS directive required"

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.PROTECT, related_name="termination_payouts"
    )
    engagement = models.OneToOneField(
        "employees.EmployeeEngagement",
        on_delete=models.PROTECT,
        related_name="termination_payout",
    )
    termination_date = models.DateField()
    termination_reason_code = models.CharField(max_length=40)
    completed_months_service = models.SmallIntegerField(default=0)
    completed_years_service = models.SmallIntegerField(default=0)
    weekly_wage_used = models.DecimalField(
        max_digits=14, decimal_places=4, default=0, help_text="Basis for notice and severance."
    )
    daily_wage_used = models.DecimalField(
        max_digits=14, decimal_places=4, default=0, help_text="Basis for leave pay-out."
    )
    monthly_wage_used = models.DecimalField(
        max_digits=14, decimal_places=4, default=0, help_text="Basis for the pro-rata bonus."
    )
    notice_weeks_required = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    notice_worked = models.BooleanField(default=True)
    notice_pay_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    accrued_leave_days = models.DecimalField(
        max_digits=8, decimal_places=3, default=0, help_text="Statutory annual leave not taken."
    )
    leave_payout_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    severance_applicable = models.BooleanField(default=False)
    severance_weeks = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    severance_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonus_applicable = models.BooleanField(default=False)
    bonus_months_worked = models.SmallIntegerField(default=0)
    bonus_pro_rata_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    outstanding_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_payout_gross = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    severance_tax_treatment = models.CharField(
        max_length=30,
        choices=SeveranceTaxTreatment.choices,
        default=SeveranceTaxTreatment.STANDARD,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    payroll_run = models.ForeignKey(
        PayrollRun,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="termination_payouts",
        help_text="The run that paid it.",
    )
    calculation_detail = models.JSONField(
        default=dict, help_text="Full working for the certificate of service pack."
    )

    class Meta:
        db_table = "termination_payout"
        ordering = ["-termination_date", "pk"]
        indexes = [models.Index(fields=["tenant", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["draft", "reviewed", "processed", "cancelled"]),
                name="termination_payout_status_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(severance_tax_treatment__in=["standard", "directive_required"]),
                name="termination_payout_severance_tax_treatment_is_known",
            ),
            # The total is its parts, proven rather than trusted.
            models.CheckConstraint(
                condition=models.Q(
                    total_payout_gross=models.F("notice_pay_amount")
                    + models.F("leave_payout_amount")
                    + models.F("severance_amount")
                    + models.F("bonus_pro_rata_amount")
                ),
                name="termination_payout_total_is_its_parts",
            ),
            # Processed means paid, and a payment names the run that made it.
            # payroll_run is nullable, so each branch says what NULL means.
            models.CheckConstraint(
                condition=models.Q(status="processed", payroll_run__isnull=False)
                | (~models.Q(status="processed") & models.Q(payroll_run__isnull=True)),
                name="termination_payout_processed_names_its_run",
            ),
        ]

    def __str__(self):
        return f"Termination payout for {self.employee_id} on {self.termination_date}"
