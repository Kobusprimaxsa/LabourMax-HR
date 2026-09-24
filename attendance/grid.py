"""The capture grid service — what the screen will call. No views, no
templates: this codebase still has no view layer, and the screen itself
belongs with the UI phase.

**Pre-fill is for salaried bases only** (D-25, already settled in
``employers.PayGroup.is_attendance_driven``). Weekly, fortnightly and monthly
pre-fill from the work schedule; hourly and daily open blank. An
attendance-driven base must be captured deliberately — a pre-filled hourly day
that nobody looked at is an invented wage.

A pre-fill proposal is NEVER a saved row. Nothing in this module writes
anything; the employer commits a proposal by calling ``attendance.capture``
themselves, one cell or a whole bulk fill at a time.

Bulk actions fill EMPTY cells only, through ``attendance.capture.capture()`` —
the same single validated path chunk 1 built, never a second write path that
bypasses it. A day already captured is left exactly as it is.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from attendance import scheduling
from attendance.capture import capture, rules_in_force
from attendance.models import AttendanceDay
from calculators.attendance import (
    AttendanceDayInput,
    AttendanceDayResult,
    AttendanceException,
    SpanDay,
    evaluate_exceptions,
)
from core.managers import tenant_context_of
from employees.models import Employee
from statutory import resolve


@dataclass(frozen=True)
class PrefillProposal:
    """What the grid suggests for an empty cell on a salaried base. Nothing is
    written — this is only ever shown, never stored.
    """

    day_type: str
    time_in: datetime.time | None
    time_out: datetime.time | None
    unpaid_break_minutes: int


@dataclass(frozen=True)
class GridCell:
    work_date: datetime.date
    day: AttendanceDay | None
    prefill: PrefillProposal | None

    @property
    def is_captured(self) -> bool:
        return self.day is not None


@dataclass(frozen=True)
class EmployeeGridRow:
    employee: Employee
    cells: tuple[GridCell, ...]
    #: This employee's own exceptions. ``AttendanceException`` carries a date and
    #: no employee, so the merged ``MonthGrid.exceptions`` cannot say WHOSE a
    #: blocking exception is — which the screen must (D-299).
    exceptions: tuple[AttendanceException, ...] = ()


@dataclass(frozen=True)
class MonthGrid:
    rows: tuple[EmployeeGridRow, ...]
    #: The live exceptions for the visible span, from chunk 1's evaluator —
    #: not re-derived here.
    exceptions: tuple[AttendanceException, ...] = field(default_factory=tuple)


def _month_bounds(month: datetime.date) -> tuple[datetime.date, datetime.date]:
    start = month.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1) - datetime.timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1, day=1) - datetime.timedelta(days=1)
    return start, end


def _prefill_for(
    employee: Employee,
    work_date: datetime.date,
    *,
    book: scheduling.ScheduleBook | None = None,
    holidays: set[datetime.date] | None = None,
) -> PrefillProposal | None:
    """``book`` and ``holidays`` let a whole month be answered from memory
    (D-300); one cell on its own reads them fresh."""
    if book is not None:
        schedule_day = book.day(work_date)
    else:
        schedule = scheduling.current_schedule(employee, work_date)
        schedule_day = scheduling.schedule_day_for(schedule, work_date)
    if schedule_day is None or not schedule_day.is_working_day:
        return None

    is_holiday = (
        work_date in holidays if holidays is not None else resolve.is_public_holiday(work_date)
    )
    if is_holiday:
        # Presumed not worked until the employer says otherwise (BCEA s18(1)):
        # paid, no times captured.
        return PrefillProposal(
            day_type=AttendanceDay.DayType.PUBLIC_HOLIDAY,
            time_in=None,
            time_out=None,
            unpaid_break_minutes=0,
        )

    return PrefillProposal(
        day_type=AttendanceDay.DayType.ORDINARY,
        time_in=schedule_day.start_time,
        time_out=schedule_day.end_time,
        unpaid_break_minutes=schedule_day.unpaid_break_minutes,
    )


def _span_day_from_model(
    day: AttendanceDay, book: scheduling.ScheduleBook | None = None
) -> SpanDay:
    """Reconstruct a calculator SpanDay from an already-bucketed, stored row —
    the exception evaluator is given what was computed, not asked to
    recompute it.
    """
    if book is not None:
        schedule, schedule_day = book.schedule(day.work_date), book.day(day.work_date)
    else:
        schedule = scheduling.current_schedule(day.employee, day.work_date)
        schedule_day = scheduling.schedule_day_for(schedule, day.work_date)

    day_input = AttendanceDayInput(
        work_date=day.work_date,
        day_type=day.day_type,
        time_in=day.time_in,
        time_out=day.time_out,
        unpaid_break_minutes=day.unpaid_break_minutes,
        is_standby=day.is_standby,
        is_ordinary_working_day=schedule_day.is_working_day if schedule_day else False,
        scheduled_ordinary_hours=(schedule_day.ordinary_hours if schedule_day else Decimal(0)),
        works_more_than_5_days_per_week=(schedule.days_per_week > 5) if schedule else False,
    )
    result = AttendanceDayResult(
        ordinary_hours=day.ordinary_hours,
        overtime_hours=day.overtime_hours,
        sunday_hours=day.sunday_hours,
        public_holiday_hours=day.public_holiday_hours,
        night_hours=day.night_hours,
        paid_hours_guaranteed=day.paid_hours_guaranteed,
        standby_hours_worked=day.standby_hours_worked,
        days_worked_equivalent=day.days_worked_equivalent,
    )
    return SpanDay(input=day_input, result=result)


def employee_exceptions(
    employee: Employee, days: list[AttendanceDay], *, book: scheduling.ScheduleBook | None = None
) -> tuple[AttendanceException, ...]:
    """The live exceptions for one employee's days, from chunk 1's evaluator.
    Shared with ``attendance/approval.py`` — one place derives these, ever.
    """
    if not days:
        return ()
    book = book or scheduling.ScheduleBook(employee)
    spans = tuple(
        _span_day_from_model(day, book) for day in sorted(days, key=lambda d: d.work_date)
    )
    # Rule sets rarely change mid-month; resolved once, against the span's
    # last date, which is the one most likely to still be current.
    rules = rules_in_force(employee, spans[-1].input.work_date)
    return evaluate_exceptions(spans, rules)


def month_grid(employees: list[Employee], month: datetime.date, *, pay_group=None) -> MonthGrid:
    """The rows and days the grid renders for ``month`` (any date within it).

    ``pay_group`` is the group the screen is showing, and decides whether a row
    is pre-filled (salaried) or opens blank (attendance-driven). Without it the
    employee's ``current_pay_group`` is used — a cache refreshed as at TODAY
    (D-107), so wrong for a past month and empty for an employee whose cache
    was never refreshed. D-298: the screen always knows the group, and passes it.

    Each cell carries either the captured day, or — for a salaried base only,
    and only where nothing is captured yet — a pre-fill proposal. Nothing is
    written by calling this.
    """
    start, end = _month_bounds(month)
    dates = list(scheduling.iter_dates(start, end))
    holidays = set(
        resolve.public_holidays_between(start, end).values_list("holiday_date", flat=True)
    )

    rows: list[EmployeeGridRow] = []
    all_exceptions: list[AttendanceException] = []

    for employee in employees:
        with tenant_context_of(employee):
            book = scheduling.ScheduleBook(employee)
            existing = {
                day.work_date: day
                for day in AttendanceDay.objects.filter(
                    employee=employee, work_date__gte=start, work_date__lte=end
                )
            }

            group = pay_group if pay_group is not None else employee.current_pay_group
            salaried = group is not None and not group.is_attendance_driven

            cells = []
            for work_date in dates:
                captured = existing.get(work_date)
                prefill = None
                if captured is None and salaried:
                    prefill = _prefill_for(employee, work_date, book=book, holidays=holidays)
                cells.append(GridCell(work_date=work_date, day=captured, prefill=prefill))

            own = employee_exceptions(employee, list(existing.values()), book=book)
            rows.append(EmployeeGridRow(employee=employee, cells=tuple(cells), exceptions=own))
            all_exceptions.extend(own)

    return MonthGrid(rows=tuple(rows), exceptions=tuple(all_exceptions))


def bulk_fill(
    employee: Employee,
    dates: list[datetime.date],
    *,
    day_type: str,
    time_in: datetime.time | None = None,
    time_out: datetime.time | None = None,
    unpaid_break_minutes: int = 0,
    source: str = AttendanceDay.Source.BULK_GRID,
) -> list[AttendanceDay]:
    """Capture the same values for every EMPTY date in ``dates``. A date that
    already carries a captured day is left untouched — never overwritten.

    Goes through ``attendance.capture.capture()`` for every write, so a bulk
    fill runs the identical validation a single capture does; there is no
    second, faster path that skips it.
    """
    with tenant_context_of(employee):
        already_captured = set(
            AttendanceDay.objects.filter(employee=employee, work_date__in=dates).values_list(
                "work_date", flat=True
            )
        )

    filled = []
    for work_date in dates:
        if work_date in already_captured:
            continue
        filled.append(
            capture(
                employee,
                work_date=work_date,
                day_type=day_type,
                time_in=time_in,
                time_out=time_out,
                unpaid_break_minutes=unpaid_break_minutes,
                source=source,
            )
        )
    return filled
