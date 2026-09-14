"""Attendance — phase P5, domain 6. ``attendance_day`` is chunk 1.

One row per employee per work date: what kind of day it was, the hours worked
and how they bucket, and whether a payroll run has since frozen it.

**Two forward references, both to tables that do not exist yet** — the same
house pattern ``core.TenantMembership.employee_id_ref`` already uses for
``employee`` ahead of P4: a nullable ``BigIntegerField`` named for what it
becomes, so the eventual FK migration is a type change on an existing column
rather than a new one.

- ``leave_application_id_ref`` becomes a real FK to ``leave_application`` in
  P6. Sheet 03's CHECK — ``(day_type='leave') = (leave_application_id IS NOT
  NULL)`` — is kept exactly, against this placeholder column. That makes a
  leave day impossible to create until P6 exists, which is honest: this
  system cannot yet say what leave was taken, so it should not let a day
  claim to be one.
- ``locked_by_payroll_run_id_ref`` becomes a real FK to ``payroll_run`` in P7,
  same treatment. ``import_batch_id`` (FK to ``attendance_import_batch``) is
  left out entirely rather than shipped as a third placeholder — that table
  is P5 chunk 3, a week away, and a column nothing populates yet is not a
  forward reference, it is a placeholder to remember to come back to.

**Locking is a trigger, not an application check** (invariant 4). Once a
payroll run finalises, the days it paid freeze — ``core/db/rls.py``'s
``no_update_when_locked()`` is the enforcement, because a REVOKE does not bind
the table owner the application connects as, the same lesson
``append_only()`` and ``lock_system_rows()`` already write up for two other
shapes of frozen row.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F

from core.audit import AuditedModel
from core.models import TenantScopedModel


class AttendanceDay(AuditedModel, TenantScopedModel):
    """One employee, one work date, what happened and what it pays."""

    class DayType(models.TextChoices):
        ORDINARY = "ordinary", "Ordinary"
        REST_DAY = "rest_day", "Rest day"
        SUNDAY = "sunday", "Sunday"
        PUBLIC_HOLIDAY = "public_holiday", "Public holiday"
        LEAVE = "leave", "Leave"
        ABSENT_UNPAID = "absent_unpaid", "Absent — unpaid"
        ABSENT_PAID = "absent_paid", "Absent — paid"
        NO_WORK_AVAILABLE = "no_work_available", "No work available"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        BULK_GRID = "bulk_grid", "Bulk grid"
        IMPORT = "import", "Import"
        SELF_SERVICE = "self_service", "Self service"
        GENERATED = "generated", "Generated"

    class Status(models.TextChoices):
        CAPTURED = "captured", "Captured"
        APPROVED = "approved", "Approved"
        LOCKED = "locked", "Locked"

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.CASCADE, related_name="attendance_days"
    )
    work_date = models.DateField(help_text="Partition key candidate (yearly) at volume.")
    workplace = models.ForeignKey(
        "employers.Workplace",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attendance_days",
        help_text="Site worked - contract cleaning.",
    )

    day_type = models.CharField(
        max_length=25, choices=DayType.choices, default=DayType.ORDINARY, db_index=True
    )
    # P6. See module docstring — the house pattern for a forward reference.
    leave_application_id_ref = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Becomes a real FK to leave_application in P6. Set only when day_type='leave'.",
    )

    time_in = models.TimeField(null=True, blank=True)
    time_out = models.TimeField(null=True, blank=True)
    unpaid_break_minutes = models.SmallIntegerField(default=0)

    ordinary_hours = models.DecimalField(
        max_digits=6, decimal_places=3, default=0, help_text="Hours at 1.0x."
    )
    overtime_hours = models.DecimalField(
        max_digits=6, decimal_places=3, default=0, help_text="Hours at the overtime multiplier."
    )
    sunday_hours = models.DecimalField(
        max_digits=6, decimal_places=3, default=0, help_text="Hours at the Sunday multiplier."
    )
    public_holiday_hours = models.DecimalField(
        max_digits=6, decimal_places=3, default=0, help_text="Hours worked on a public holiday."
    )
    night_hours = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=0,
        help_text="Hours inside the night-work window. A tag on the buckets above, not additive.",
    )
    paid_hours_guaranteed = models.DecimalField(
        max_digits=6, decimal_places=3, default=0, help_text="SD1 six-hour guarantee top-up."
    )
    days_worked_equivalent = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        default=0,
        help_text=(
            "1.000 for a full day, 0.500 for a half day - drives daily-rate pay and leave accrual."
        ),
    )

    is_standby = models.BooleanField(default=False, help_text="SD7 standby shift.")
    standby_hours_worked = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=0,
        help_text="Actual work performed while on standby.",
    )

    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CAPTURED, db_index=True
    )
    # P7. See module docstring.
    locked_by_payroll_run_id_ref = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Becomes a real FK to payroll_run in P7. Set when a run finalises; blocks edits.",
    )

    comment = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "attendance_day"
        ordering = ["employee_id", "-work_date"]
        indexes = [
            models.Index(fields=["tenant", "work_date"]),
            models.Index(fields=["employee", "work_date", "status"]),
            models.Index(fields=["workplace", "work_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "work_date"], name="uniq_attendance_day_per_employee"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    day_type__in=[
                        "ordinary",
                        "rest_day",
                        "sunday",
                        "public_holiday",
                        "leave",
                        "absent_unpaid",
                        "absent_paid",
                        "no_work_available",
                    ]
                ),
                name="attendance_day_day_type_is_known",
            ),
            # night_hours, paid_hours_guaranteed and standby_hours_worked are
            # deliberately NOT in this sum - each measures a portion of, or a
            # top-up beside, the four buckets that make up the worked day.
            # Summing them in as well would double-count the same hour twice.
            models.CheckConstraint(
                condition=models.Q(
                    ordinary_hours__lte=(
                        24 - F("overtime_hours") - F("sunday_hours") - F("public_holiday_hours")
                    )
                ),
                name="attendance_day_hours_sum_under_24",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    ordinary_hours__gte=0,
                    overtime_hours__gte=0,
                    sunday_hours__gte=0,
                    public_holiday_hours__gte=0,
                    night_hours__gte=0,
                    paid_hours_guaranteed__gte=0,
                    standby_hours_worked__gte=0,
                ),
                name="attendance_day_hours_are_not_negative",
            ),
            # A leave day names the leave it is; nothing else may. Kept against
            # the P6 placeholder deliberately - see the module docstring.
            models.CheckConstraint(
                condition=(
                    models.Q(day_type="leave", leave_application_id_ref__isnull=False)
                    | (
                        ~models.Q(day_type="leave")
                        & models.Q(leave_application_id_ref__isnull=True)
                    )
                ),
                name="attendance_day_leave_needs_a_leave_application",
            ),
        ]

    def __str__(self):
        return f"{self.employee_id} on {self.work_date} ({self.day_type})"
