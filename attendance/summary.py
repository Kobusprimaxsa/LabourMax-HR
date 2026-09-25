"""timesheet_summary — a cache, and nothing more (invariant 3).

Every figure is recomputed from ``attendance_day``, every time. There is no
incremental update path: an incrementally-maintained cache that has drifted
cannot be told apart from a correct one, so the only thing trusted is a fresh
rebuild.

**Staleness, two layers** (D-153):

- the FAST PATH — ``attendance/staleness.py``'s signal sets ``is_stale`` the
  moment an ``attendance_day`` in the covered period is saved or deleted. A
  signal rather than a check inside ``attendance/capture.py``, so the bulk
  importer that arrives in chunk 3, and any future writer, are covered by
  construction rather than by a call site somebody has to remember to add.
- the GROUND TRUTH — ``is_actually_stale()`` below, which never looks at the
  flag at all: it compares ``computed_at`` against the covered days' own
  ``updated_at``, straight from the data. A flag that was missed, or cleared
  by hand, is still detectable this way — a missed flag is a bug to find, not
  a permanent wrong answer.

The one gap this pair does not close: a day DELETED from the period without
the signal firing (which nothing in this codebase would legitimately do —
``AttendanceDay`` rows are only ever removed through the ORM, which always
fires ``post_delete``) leaves no trace in the remaining days' timestamps for
``is_actually_stale()`` to find. The signal is what actually catches deletion;
the ground truth function is a check on UPDATES going missing, not a complete
replacement for the signal existing at all.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from attendance import scheduling
from attendance.models import AttendanceDay, TimesheetSummary
from core.managers import tenant_context_of
from employees.models import Employee
from payroll.models import PayPeriod

ZERO = Decimal("0")

#: day_types counted as a day worked, or at least a day the employee was
#: available for and guaranteed pay on. Leave and absence are counted
#: separately below.
_WORKED_FOR_TOTALS = frozenset(
    {
        AttendanceDay.DayType.ORDINARY,
        AttendanceDay.DayType.REST_DAY,
        AttendanceDay.DayType.SUNDAY,
        AttendanceDay.DayType.PUBLIC_HOLIDAY,
        AttendanceDay.DayType.NO_WORK_AVAILABLE,
    }
)
_PAID_LEAVE_TYPES = frozenset({AttendanceDay.DayType.LEAVE, AttendanceDay.DayType.ABSENT_PAID})


def _period_days(employee: Employee, pay_period: PayPeriod):
    return AttendanceDay.objects.filter(
        employee=employee,
        work_date__gte=pay_period.period_start,
        work_date__lte=pay_period.period_end,
    )


def _hours_actually_worked(day: AttendanceDay) -> Decimal:
    """Hours this employee was at work for, whichever buckets they landed in.

    Deliberately NOT ``days_worked_equivalent``, which is 1.000 for a leave day
    and for a paid absence — neither of which is a day anybody worked. Standby
    is included: a standby occasion is time the employee gave the employer.
    """
    return (
        day.ordinary_hours
        + day.overtime_hours
        + day.sunday_hours
        + day.public_holiday_hours
        + day.standby_hours_worked
    )


def _count_public_holidays_not_worked(
    employee: Employee, pay_period: PayPeriod, days: list[AttendanceDay]
) -> Decimal:
    """Public holidays in the period, falling on an ordinary working day, that
    were not worked. Read the schedule, not the calendar — a high earner above
    the BCEA earnings threshold loses s18(3) but KEEPS this entitlement, so it
    is never gated on earnings, only on the schedule.
    """
    # WORKED, whatever day_type the capture carries (D-280). This used to read
    # day_type == PUBLIC_HOLIDAY, which was safe only while a public holiday
    # could never also be something else. Since s2(1) adds a Monday without
    # taking the Sunday away, 9 August 2026 is BOTH a Sunday and a public
    # holiday, and a contract cleaner who works it is naturally captured as
    # SUNDAY. Counting that date as "not worked" would pay the s18(2)(a)
    # unworked-holiday day on top of the Sunday premium, for a day the employee
    # was at work.
    worked_dates = {day.work_date for day in days if _hours_actually_worked(day) > 0}

    count = 0
    # This EMPLOYER's holidays (D-319): a substitute day counts, and a day
    # exchanged away does not.
    from leave.holidays import pay_holidays

    for holiday_date in pay_holidays(
        employee.employer, pay_period.period_start, pay_period.period_end
    ):
        if holiday_date in worked_dates:
            continue
        schedule = scheduling.current_schedule(employee, holiday_date)
        schedule_day = scheduling.schedule_day_for(schedule, holiday_date)
        if schedule_day is not None and schedule_day.is_working_day:
            count += 1
    return Decimal(count)


def recompute_summary(employee: Employee, pay_period: PayPeriod) -> TimesheetSummary:
    """Rebuild the summary for this employee and period from scratch. Idempotent —
    calling this twice in a row with no change to the underlying days produces
    identical figures, because both runs read the same rows the same way.
    """
    with transaction.atomic(), tenant_context_of(employee):
        days = list(_period_days(employee, pay_period))

        totals = {
            "total_ordinary_hours": sum((d.ordinary_hours for d in days), ZERO),
            "total_overtime_hours": sum((d.overtime_hours for d in days), ZERO),
            "total_sunday_hours": sum((d.sunday_hours for d in days), ZERO),
            "total_public_holiday_hours": sum((d.public_holiday_hours for d in days), ZERO),
            "total_night_hours": sum((d.night_hours for d in days), ZERO),
            "total_standby_shifts": sum(1 for d in days if d.is_standby),
            "total_days_worked": sum(
                (d.days_worked_equivalent for d in days if d.day_type in _WORKED_FOR_TOTALS), ZERO
            ),
            "total_paid_leave_days": sum(
                (d.days_worked_equivalent for d in days if d.day_type in _PAID_LEAVE_TYPES), ZERO
            ),
            "total_unpaid_days": Decimal(
                sum(1 for d in days if d.day_type == AttendanceDay.DayType.ABSENT_UNPAID)
            ),
            "total_public_holidays_not_worked": _count_public_holidays_not_worked(
                employee, pay_period, days
            ),
        }

        summary, _created = TimesheetSummary.objects.update_or_create(
            tenant=employee.tenant,
            employee=employee,
            pay_period=pay_period,
            defaults={**totals, "computed_at": timezone.now(), "is_stale": False},
        )
    return summary


def is_actually_stale(summary: TimesheetSummary) -> bool:
    """The ground truth, independent of ``is_stale``: has any covered day
    changed since this summary was computed? See the module docstring.
    """
    with tenant_context_of(summary):
        latest = _period_days(summary.employee, summary.pay_period).aggregate(
            latest=Max("updated_at")
        )["latest"]
    if latest is None:
        return False
    return latest > summary.computed_at
