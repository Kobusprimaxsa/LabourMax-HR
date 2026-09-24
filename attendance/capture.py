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

from attendance import scheduling
from attendance.models import AttendanceDay
from calculators.attendance import AttendanceDayInput, RuleFigures, bucket_day
from core.managers import tenant_context_of
from employees.models import Employee
from statutory import resolve


class AttendanceCaptureRefusedError(Exception):
    """The day may not be written this way. Nothing was written."""


class ApprovedDayError(AttendanceCaptureRefusedError):
    """The day is APPROVED and the caller did not say to replace it (D-297)."""


class LockedDayError(AttendanceCaptureRefusedError):
    """The day is locked by a finalised payroll run."""


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


def _public_holiday_warning(work_date: datetime.date, day_type: str, worked: bool) -> str | None:
    """A day captured as something other than a public holiday, on a date the
    calendar says is one (D-280).

    **A warning, not a refusal, and it does not re-bucket anything.** Since
    s2(1) adds a Monday without taking the Sunday away, a date can be a Sunday
    and a public holiday at once — 9 August 2026 is both — and
    ``attendance_day.day_type`` holds exactly one value. Which of s16 and s18
    prices such a day, or whether s18(2)(b)(ii)'s "the amount earned by the
    employee for the work performed on that day" means the SUNDAY amount and so
    stacks the two, is a reading of the Act that nothing in this build has
    settled (O-40). Choosing one here would be inventing the answer.

    What can be said without a reading is that s18 is not optional: a public
    holiday captured as an ordinary or a Sunday day is priced as if the holiday
    were not there. So this names the date and leaves the decision with a
    person, exactly as D-157's night-hours warning does.
    """
    if not worked:
        # s18(2)(a) - a holiday NOT worked is paid from the schedule, by
        # attendance/summary.py, which reads the calendar itself rather than
        # this row's day_type. Nothing is lost by staying quiet.
        return None
    if day_type == AttendanceDay.DayType.PUBLIC_HOLIDAY:
        return None
    holiday = resolve.public_holiday_on(work_date)
    if holiday is None:
        return None
    return (
        f"{work_date:%d %B %Y} is a public holiday ({holiday.name}) and this day was "
        f"captured as '{day_type}'. BCEA s18(2)(b) prices a worked public holiday and "
        f"nothing here applies it to a day captured as anything else. A date can be "
        f"both — s2(1) adds a Monday without taking the Sunday away, so 9 August 2026 "
        f"is a Sunday AND a public holiday — and whether s16 and s18 stack on such a "
        f"day is unread (O-40). Capture it as a public holiday, or decide deliberately "
        f"not to."
    )


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
    leave_application=None,
    leave_day_portion: Decimal = Decimal(1),
    source: str = AttendanceDay.Source.MANUAL,
    comment: str = "",
    import_batch=None,
    hours_worked: Decimal | None = None,
    allow_replacing_approved: bool = False,
) -> AttendanceDay:
    """Create or update the day for this employee and date. Atomic.

    Buckets the hours from the rule set in force on ``work_date`` and stores
    the result — it is never recomputed later. Refuses, naming the payroll
    run, if the existing day is locked; refuses if the reference data needed
    to bucket it is not loaded. Writes nothing in either case.

    ``hours_worked`` is a raw total, an alternative to ``time_in``/``time_out``
    for a caller that captured a plain figure rather than clock times (the
    bulk import's "hours worked" column, D-156). Ignored when both time_in and
    time_out are given — a captured time span always wins.

    The returned row carries a non-persisted ``capture_warnings`` attribute —
    the calculator's own warnings (D-157: a bare hours total whose night hours
    could not honestly be computed, among others), plus this module's own
    (D-280: a worked day on a public holiday captured as something else). Not a
    column: nothing here is stored, only surfaced to whichever caller just
    wrote the day, so the grid and the importer report the same thing from the
    same call.
    """
    with transaction.atomic(), tenant_context_of(employee):
        existing = AttendanceDay.objects.filter(employee=employee, work_date=work_date).first()
        if (
            existing is not None
            and existing.status == AttendanceDay.Status.APPROVED
            and not allow_replacing_approved
        ):
            # D-297. Approval is a human judgement over THESE values; writing new
            # ones under it would leave an approval standing over figures nobody
            # approved. The importer always refused this by its own flag; now
            # the one write path does, for every caller.
            raise ApprovedDayError(
                f"{work_date:%d %B %Y} for {employee} was approved, so it is not changed "
                f"quietly. Overwriting it on purpose withdraws the approval, and the new "
                f"figures then need approving again."
            )
        if existing is not None and existing.status == AttendanceDay.Status.LOCKED:
            raise LockedDayError(
                f"{work_date:%d %B %Y} for {employee} is locked by payroll run "
                f"{existing.locked_by_payroll_run_id_ref} and cannot be changed. A "
                f"correction reverses and replaces that run instead (CLAUDE.md "
                f"invariant 4)."
            )

        rules = rules_in_force(employee, work_date)

        schedule = scheduling.current_schedule(employee, work_date)
        schedule_day = scheduling.schedule_day_for(schedule, work_date)

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
            hours_worked=hours_worked,
            scheduled_start_time=schedule_day.start_time if schedule_day else None,
            scheduled_end_time=schedule_day.end_time if schedule_day else None,
            leave_day_portion=leave_day_portion,
        )
        result = bucket_day(day_input, rules)
        warnings = list(result.warnings)
        holiday_warning = _public_holiday_warning(
            work_date,
            day_type,
            worked=(
                result.ordinary_hours
                + result.overtime_hours
                + result.sunday_hours
                + result.public_holiday_hours
                + result.standby_hours_worked
            )
            > 0,
        )
        if holiday_warning is not None:
            warnings.append(holiday_warning)

        fields = {
            "tenant": employee.tenant,
            "employee": employee,
            "work_date": work_date,
            "workplace": workplace,
            "day_type": day_type,
            "leave_application": leave_application,
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
            "import_batch": import_batch,
        }

        if existing is not None:
            for key, value in fields.items():
                setattr(existing, key, value)
            if existing.status == AttendanceDay.Status.APPROVED:
                # Replaced on purpose: the approval covered the OLD values.
                existing.status = AttendanceDay.Status.CAPTURED
            existing.full_clean()
            existing.save()
            existing.capture_warnings = tuple(warnings)
            return existing

        day = AttendanceDay(**fields)
        day.full_clean()
        day.save()
        day.capture_warnings = tuple(warnings)
        return day
