"""Schedule lookups shared across attendance — capture, the grid, completeness
and the summary cache all need to answer the same question: what does this
employee's own schedule say about this date?
"""

from __future__ import annotations

import datetime

from employees.models import Employee, WorkSchedule, WorkScheduleDay


def current_schedule(employee: Employee, on_date: datetime.date) -> WorkSchedule | None:
    return (
        WorkSchedule.objects.filter(employee=employee, effective_from__lte=on_date)
        .exclude(effective_to__lte=on_date)
        .order_by("-effective_from")
        .first()
    )


def cycle_day_for(schedule: WorkSchedule, on_date: datetime.date) -> int:
    """Which day of the schedule's cycle a date falls on.

    For the ordinary 7-day cycle, day 0 is Monday (the schedule's own
    documented convention) — calendar weekday, independent of when the
    schedule itself started. Any other cycle length (only 14 is modelled, and
    the WorkSchedule docstring itself says that is unexercised by the
    product) has no such calendar anchor, so it falls back to counting from
    the schedule's own effective_from.
    """
    if schedule.cycle_length_days == 7:
        return on_date.weekday()
    return (on_date - schedule.effective_from).days % schedule.cycle_length_days


def schedule_day_for(
    schedule: WorkSchedule | None, on_date: datetime.date
) -> WorkScheduleDay | None:
    if schedule is None:
        return None
    return WorkScheduleDay.objects.filter(
        work_schedule=schedule, cycle_day=cycle_day_for(schedule, on_date)
    ).first()


def iter_dates(start: datetime.date, end: datetime.date):
    """Every calendar date from start to end, inclusive of both ends."""
    current = start
    one_day = datetime.timedelta(days=1)
    while current <= end:
        yield current
        current += one_day


class ScheduleBook:
    """One employee's schedules and schedule days, read ONCE, answered per date
    in memory — the same answers ``current_schedule()`` and
    ``schedule_day_for()`` give, without two queries per date.

    A month's grid asks for every day of every employee; answered one query at
    a time that was 1 320 queries for twenty employees, and the exceptions
    panel took 2.7 seconds to redraw after each save (D-300's measurement).
    Call with the employee's tenant pinned.
    """

    def __init__(self, employee: Employee):
        self.schedules = list(
            WorkSchedule.objects.filter(employee=employee)
            .prefetch_related("days")
            .order_by("-effective_from")
        )
        self.days = {
            schedule.pk: {day.cycle_day: day for day in schedule.days.all()}
            for schedule in self.schedules
        }

    def schedule(self, on_date: datetime.date) -> WorkSchedule | None:
        return next(
            (
                s
                for s in self.schedules
                if s.effective_from <= on_date
                and (s.effective_to is None or s.effective_to > on_date)
            ),
            None,
        )

    def day(self, on_date: datetime.date) -> WorkScheduleDay | None:
        schedule = self.schedule(on_date)
        if schedule is None:
            return None
        return self.days[schedule.pk].get(cycle_day_for(schedule, on_date))
