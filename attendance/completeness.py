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

from attendance import scheduling
from attendance.models import AttendanceDay
from core.managers import tenant_context_of
from employees.models import Employee


def missing_attendance_days(employee: Employee, period) -> list[datetime.date]:
    """Scheduled working days in ``period`` (anything with ``period_start`` and
    ``period_end`` — a ``PayPeriod`` in practice) with no ``attendance_day``
    row, for an attendance-driven pay basis. Returns an empty list outright
    for a salaried employee — there is nothing to be missing.
    """
    with tenant_context_of(employee):
        pay_group = employee.current_pay_group
        if pay_group is None or not pay_group.is_attendance_driven:
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
            schedule = scheduling.current_schedule(employee, work_date)
            schedule_day = scheduling.schedule_day_for(schedule, work_date)
            if (
                schedule_day is not None
                and schedule_day.is_working_day
                and work_date not in existing_dates
            ):
                missing.append(work_date)
        return missing
