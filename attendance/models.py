"""Attendance — phase P5, domain 6. ``attendance_day`` is chunk 1.

One row per employee per work date: what kind of day it was, the hours worked
and how they bucket, and whether a payroll run has since frozen it.

**One forward reference is now a real FK; one still waits** — the house
pattern ``core.TenantMembership.employee_id_ref`` already uses for
``employee`` ahead of P4: a nullable placeholder named for what it becomes,
so the eventual FK migration is a type change on an existing column rather
than a new one.

- ``leave_application`` is now a real FK to ``leave.LeaveApplication`` (P6
  chunk 2) — it was ``leave_application_id_ref``, a ``BigIntegerField``,
  until that table existed. Sheet 03's CHECK — ``(day_type='leave') =
  (leave_application_id IS NOT NULL)`` — is kept exactly, now against the
  real column. Only ``leave/authorisation.py`` approving an application ever
  sets it; nothing else writes a ``day_type='leave'`` row.
- ``locked_by_payroll_run_id_ref`` stays a placeholder — becomes a real FK to
  ``payroll_run`` in P7.

``import_batch`` (FK to ``attendance_import_batch``) arrives in chunk 3, once
that table exists. Unlike the two forward references above, this one is a real
FK from day one — the table it points at is built in the same chunk, so there
is no placeholder period to bridge.

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
from django.utils import timezone

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
    # P6 chunk 2. See module docstring — a real FK now, was a BigIntegerField
    # placeholder through P5 and P6 chunk 1.
    leave_application = models.ForeignKey(
        "leave.LeaveApplication",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attendance_days",
        help_text="Set only when day_type='leave', by leave/authorisation.py's approve().",
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

    import_batch = models.ForeignKey(
        "attendance.AttendanceImportBatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="days",
        editable=False,
        help_text=(
            "Set only by the bulk importer, and only for as long as that batch's "
            "write stands. Reverse restores a replaced day to what it held before "
            "and clears this back to whatever it named then (D-155)."
        ),
    )

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
            # A leave day names the leave it is; nothing else may. Sheet 03's
            # CHECK, now against the real FK (P6 chunk 2) rather than the
            # placeholder it was kept against through P5 and P6 chunk 1.
            models.CheckConstraint(
                condition=(
                    models.Q(day_type="leave", leave_application__isnull=False)
                    | (~models.Q(day_type="leave") & models.Q(leave_application__isnull=True))
                ),
                name="attendance_day_leave_needs_a_leave_application",
            ),
        ]

    def __str__(self):
        return f"{self.employee_id} on {self.work_date} ({self.day_type})"


class TimesheetSummary(AuditedModel, TenantScopedModel):
    """One employee's totals for one pay period. A CACHE, and nothing more
    (invariant 3).

    Every figure here is recomputable from ``attendance_day`` — nothing reads
    this table that could not instead read the days it summarises, and it is
    NEVER edited directly. There is no incremental update path: recomputation
    always rebuilds from scratch (``attendance/summary.py::recompute_summary``),
    because an incrementally-updated cache that has drifted cannot be told
    apart from a correct one — the only way to trust a total is to have just
    derived it fresh. A stored total is exactly the kind of number somebody
    later "corrects" by hand; do not.

    **Staleness is answered two ways, deliberately** (D-153). ``is_stale`` is a
    fast flag, set by a signal the moment an ``attendance_day`` in this period
    is saved or deleted — a signal rather than a service-layer check, so the
    bulk importer in chunk 3 and every future writer are covered by
    construction, with no call site to remember. ``attendance/summary.py``'s
    ``is_actually_stale()`` is the ground truth: it compares ``computed_at``
    against the covered days' own ``updated_at``, straight from the data, so a
    flag that was somehow missed — or cleared by hand — is still detectable
    rather than permanently wrong.
    """

    employee = models.ForeignKey(
        "employees.Employee", on_delete=models.CASCADE, related_name="timesheet_summaries"
    )
    pay_period = models.ForeignKey(
        "payroll.PayPeriod", on_delete=models.PROTECT, related_name="timesheet_summaries"
    )

    total_ordinary_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    total_overtime_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    total_sunday_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    total_public_holiday_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    total_night_hours = models.DecimalField(max_digits=9, decimal_places=3, default=0)
    total_standby_shifts = models.SmallIntegerField(default=0)

    total_days_worked = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    total_paid_leave_days = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    total_unpaid_days = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    total_public_holidays_not_worked = models.DecimalField(
        max_digits=7,
        decimal_places=3,
        default=0,
        help_text="Paid if they fall on an ordinary working day.",
    )

    computed_at = models.DateTimeField(default=timezone.now)
    is_stale = models.BooleanField(
        default=False, help_text="Set when an underlying attendance_day changes."
    )

    class Meta:
        db_table = "timesheet_summary"
        ordering = ["employee_id", "-pay_period_id"]
        indexes = [models.Index(fields=["tenant", "pay_period"])]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "pay_period"], name="uniq_timesheet_summary_per_employee_period"
            ),
        ]

    def __str__(self):
        return f"{self.employee_id} for period {self.pay_period_id}"


class AttendanceImportBatch(AuditedModel, TenantScopedModel):
    """One bulk attendance upload for one period, previewed then applied as a
    unit — sheet 02 and sheet 03, P5 chunk 3.

    **The employee import only ever creates; this one routinely replaces.**
    Re-importing a corrected file is the ordinary case, not an edge case, so
    ``attendance/importing.py`` distinguishes a day it is CREATING from one it
    is REPLACING, and refuses a locked day by name in the preview and an
    approved day unless the caller explicitly allows it (D-156).

    ``prior_state`` is NOT in sheet 02 (D-156). Reverse for the employee
    import deletes, because every row it touched was new. This import
    replaces days that already held the employer's own earlier, legitimate
    capture — a reverse that only deleted would throw that away, which is not
    the batch's to discard. So every REPLACE snapshots the day's own prior
    values here before writing over them, and reverse restores each one
    exactly rather than merely removing it.
    """

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        VALIDATING = "validating", "Validating"
        PREVIEW = "preview", "Preview"
        APPLIED = "applied", "Applied"
        REVERSED = "reversed", "Reversed"
        FAILED = "failed", "Failed"

    employer = models.ForeignKey(
        "employers.Employer", on_delete=models.PROTECT, related_name="attendance_import_batches"
    )
    source_file = models.ForeignKey(
        "core.FileObject",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The uploaded spreadsheet. Content is purged once applied or reversed (D-141).",
    )

    period_start = models.DateField()
    period_end = models.DateField()

    row_count = models.IntegerField(default=0)
    accepted_count = models.IntegerField(default=0)
    rejected_count = models.IntegerField(default=0)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.UPLOADED, db_index=True
    )
    validation_report = models.JSONField(
        default=list, help_text="Per-row errors and warnings. Never a full ID or bank number."
    )
    prior_state = models.JSONField(
        default=list,
        help_text=(
            "D-156. One entry per day this batch REPLACED: the employee id, the work "
            "date, and every mutable attendance_day column's value before the "
            "replace. Empty for a day the batch created outright — reverse deletes "
            "those instead of restoring them."
        ),
    )

    applied_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "attendance_import_batch"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["period_start", "period_end"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "uploaded",
                        "validating",
                        "preview",
                        "applied",
                        "reversed",
                        "failed",
                    ]
                ),
                name="attendance_import_batch_status_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="attendance_import_batch_period_end_after_start",
            ),
        ]

    def __str__(self):
        return f"Attendance import batch {self.pk} ({self.status})"
