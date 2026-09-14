"""Capturing a day's attendance — the employees'-side entry point.

Buckets are computed HERE, at capture, from the ``working_time_rule_set`` row
in force on the work date, and STORED — never recomputed on read. A March day
read again in 2029 must show what March's rules produced; recomputing would
silently rewrite history the moment a rule set is superseded (invariant 2,
and the reason reference data is never updated in place, only superseded by a
new effective-dated row).

Refuses to write a locked day, naming the payroll run, before the write is
even attempted — the trigger in ``core/db/rls.py`` is the backstop for every
path that does not come through here, this is the friendly front door.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db import transaction

from attendance.models import AttendanceDay
from calculators.attendance import AttendanceDayInput, RuleFigures, bucket_day
from core.managers import tenant_context_of
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from statutory import resolve


class AttendanceCaptureRefusedError(Exception):
    """The day may not be written this way. Nothing was written."""


class LockedDayError(AttendanceCaptureRefusedError):
    """The day is locked by a finalised payroll run."""


def _current_schedule(employee: Employee, on_date: datetime.date) -> WorkSchedule | None:
    return (
        WorkSchedule.objects.filter(employee=employee, effective_from__lte=on_date)
        .exclude(effective_to__lte=on_date)
        .order_by("-effective_from")
        .first()
    )


def _cycle_day_for(schedule: WorkSchedule, on_date: datetime.date) -> int:
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


def _schedule_day_for(
    schedule: WorkSchedule | None, on_date: datetime.date
) -> WorkScheduleDay | None:
    if schedule is None:
        return None
    return WorkScheduleDay.objects.filter(
        work_schedule=schedule, cycle_day=_cycle_day_for(schedule, on_date)
    ).first()


def _rule_figures(row) -> RuleFigures:
    """The calculator's frozen figures, read off a working_time_rule_set row."""
    return RuleFigures(
        ordinary_hours_per_week=row.ordinary_hours_per_week,
        ordinary_hours_per_day_5day=row.ordinary_hours_per_day_5day,
        ordinary_hours_per_day_6day=row.ordinary_hours_per_day_6day,
        overtime_multiplier=row.overtime_multiplier,
        max_overtime_hours_per_day=row.max_overtime_hours_per_day,
        max_overtime_hours_per_week=row.max_overtime_hours_per_week,
        sunday_multiplier_ordinary=row.sunday_multiplier_ordinary,
        sunday_multiplier_non_ordinary=row.sunday_multiplier_non_ordinary,
        public_holiday_worked_multiplier=row.public_holiday_worked_multiplier,
        public_holiday_not_worked_paid=row.public_holiday_not_worked_paid,
        night_work_start_time=row.night_work_start_time,
        night_work_end_time=row.night_work_end_time,
        night_allowance_type=row.night_allowance_type,
        night_allowance_value=row.night_allowance_value,
        standby_allowance_per_shift=row.standby_allowance_per_shift,
        standby_window_start=row.standby_window_start,
        standby_window_end=row.standby_window_end,
        standby_hours_before_overtime=row.standby_hours_before_overtime,
        min_paid_hours_per_day=row.min_paid_hours_per_day,
        meal_interval_after_hours=row.meal_interval_after_hours,
        meal_interval_minutes=row.meal_interval_minutes,
        daily_rest_hours=row.daily_rest_hours,
        weekly_rest_hours=row.weekly_rest_hours,
    )


def rules_in_force(employee: Employee, on_date: datetime.date) -> RuleFigures:
    """The figures the calculator needs, for this employee's sector on this date.

    Raises ``AttendanceCaptureRefusedError`` rather than a bare
    ``StatutoryValueMissingError`` — this refuses the same way every other
    capture path in this codebase refuses when reference data is missing.
    """
    try:
        row = resolve.working_time_rules(employee.employer.sector, on_date)
    except resolve.StatutoryValueMissingError as error:
        raise AttendanceCaptureRefusedError(
            f"No working time rules are loaded for {on_date:%d %B %Y}, so this day "
            f"cannot be bucketed. Run `python manage.py loadstatutory --all`."
        ) from error
    return _rule_figures(row)


def capture(
    employee: Employee,
    *,
    work_date: datetime.date,
    day_type: str,
    workplace=None,
    time_in: datetime.time | None = None,
    time_out: datetime.time | None = None,
    unpaid_break_minutes: int = 0,
    is_standby: bool = False,
    leave_application_id_ref: int | None = None,
    source: str = AttendanceDay.Source.MANUAL,
    comment: str = "",
) -> AttendanceDay:
    """Create or update the day for this employee and date. Atomic.

    Buckets the hours from the rule set in force on ``work_date`` and stores
    the result — it is never recomputed later. Refuses, naming the payroll
    run, if the existing day is locked; refuses if the reference data needed
    to bucket it is not loaded. Writes nothing in either case.
    """
    with transaction.atomic(), tenant_context_of(employee):
        existing = AttendanceDay.objects.filter(employee=employee, work_date=work_date).first()
        if existing is not None and existing.status == AttendanceDay.Status.LOCKED:
            raise LockedDayError(
                f"{work_date:%d %B %Y} for {employee} is locked by payroll run "
                f"{existing.locked_by_payroll_run_id_ref} and cannot be changed. A "
                f"correction reverses and replaces that run instead (CLAUDE.md "
                f"invariant 4)."
            )

        rules = rules_in_force(employee, work_date)

        schedule = _current_schedule(employee, work_date)
        schedule_day = _schedule_day_for(schedule, work_date)

        day_input = AttendanceDayInput(
            work_date=work_date,
            day_type=day_type,
            time_in=time_in,
            time_out=time_out,
            unpaid_break_minutes=unpaid_break_minutes,
            is_standby=is_standby,
            is_ordinary_working_day=schedule_day.is_working_day if schedule_day else False,
            scheduled_ordinary_hours=(schedule_day.ordinary_hours if schedule_day else Decimal(0)),
            works_more_than_5_days_per_week=(
                schedule.days_per_week > 5 if schedule is not None else False
            ),
        )
        result = bucket_day(day_input, rules)

        fields = {
            "tenant": employee.tenant,
            "employee": employee,
            "work_date": work_date,
            "workplace": workplace,
            "day_type": day_type,
            "leave_application_id_ref": leave_application_id_ref,
            "time_in": time_in,
            "time_out": time_out,
            "unpaid_break_minutes": unpaid_break_minutes,
            "ordinary_hours": result.ordinary_hours,
            "overtime_hours": result.overtime_hours,
            "sunday_hours": result.sunday_hours,
            "public_holiday_hours": result.public_holiday_hours,
            "night_hours": result.night_hours,
            "paid_hours_guaranteed": result.paid_hours_guaranteed,
            "days_worked_equivalent": result.days_worked_equivalent,
            "is_standby": is_standby,
            "standby_hours_worked": result.standby_hours_worked,
            "source": source,
            "comment": comment,
        }

        if existing is not None:
            for key, value in fields.items():
                setattr(existing, key, value)
            existing.full_clean()
            existing.save()
            return existing

        day = AttendanceDay(**fields)
        day.full_clean()
        day.save()
        return day
