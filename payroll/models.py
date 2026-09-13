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
