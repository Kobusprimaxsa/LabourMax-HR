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

from django.db import models

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

    ``payslip_id_ref`` is a plain BigIntegerField: ``payslip`` does not exist
    yet, and P7's assembly is blocked on P2 verification. It becomes a real
    foreign key in the chunk that builds the payslip — the same forward-reference
    pattern ``attendance_day`` used for ``leave_application`` (D-151's chunk).
    """

    payslip_id_ref = models.BigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Becomes a real FK to payslip when P7's assembly is built.",
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
