"""The P7 hook: what attendance is missing before a payroll run can trust this
employee's period. P7's validation gate calls this; it is not built here.

**Attendance-driven bases only** (hourly and daily). For a salaried base, the
absence of a row means an ordinary day worked — the original brief's rule: a
monthly employee is assumed to have worked the full month less recorded
unpaid leave. Getting this backwards either blocks every salaried payroll run
on missing rows nobody was ever going to capture, or pays an hourly worker
for days nobody recorded them working.
"""

from __future__ import annotations

import datetime

from django.db.models import Q

from attendance import scheduling
from attendance.models import AttendanceDay
from core.managers import tenant_context_of
from employees.models import Employee, EmployeeRemuneration


def missing_attendance_days(employee: Employee, period) -> list[datetime.date]:
    """Scheduled working days in ``period`` (anything with ``period_start`` and
    ``period_end`` — a ``PayPeriod`` in practice) with no ``attendance_day``
    row, for an attendance-driven pay basis. Returns an empty list outright
    for a salaried employee — there is nothing to be missing.

    **Read from the rows in force ON EACH DATE, never the employee's cache**
    (D-314). This used to ask ``employee.current_pay_group``, which is refreshed
    as at TODAY (D-107): an employee whose cache had not been refreshed, or who
    had moved to a monthly pay group since, got no check at all, and an
    uncaptured hourly day went through the payroll gate unannounced. And only
    days inside the engagement count — a leaver's days after their last one are
    not missing, they are not owed.
    """
    with tenant_context_of(employee):
        rows = list(
            EmployeeRemuneration.objects.filter(
                employee=employee, effective_from__lte=period.period_end
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=period.period_start))
            .select_related("pay_group", "engagement")
        )
        if not any(row.pay_group.is_attendance_driven for row in rows):
            return []
        existing_dates = set(
            AttendanceDay.objects.filter(
                employee=employee,
                work_date__gte=period.period_start,
                work_date__lte=period.period_end,
            ).values_list("work_date", flat=True)
        )
        missing = []
        for work_date in scheduling.iter_dates(period.period_start, period.period_end):
            row = next(
                (
                    row
                    for row in rows
                    if row.effective_from <= work_date
                    and (row.effective_to is None or work_date < row.effective_to)
                ),
                None,
            )
            if row is None or not row.pay_group.is_attendance_driven:
                continue
            engagement = row.engagement
            if work_date < engagement.start_date or (
                engagement.termination_date is not None and work_date > engagement.termination_date
            ):
                continue
            schedule = scheduling.current_schedule(employee, work_date)
            schedule_day = scheduling.schedule_day_for(schedule, work_date)
            if (
                schedule_day is not None
                and schedule_day.is_working_day
                and work_date not in existing_dates
            ):
                missing.append(work_date)
        return missing
