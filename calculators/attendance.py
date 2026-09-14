"""Hour bucketing — THE FIRST REAL CALCULATOR. Pure functions only.

No ORM import, no database access, no file I/O, no ``datetime.now()``. Every
function here takes a frozen input structure and a frozen set of statutory
figures, and returns a frozen result. The caller — ``attendance/capture.py`` —
reads ``employee``, ``work_schedule``, ``work_schedule_day`` and
``working_time_rule_set`` (through ``statutory.resolve``, for the rule set IN
FORCE ON THE WORK DATE) and assembles both. Nothing in this module knows a
table exists.

**Not one statutory figure appears here.** Every multiplier, cap, window and
minimum is a field on :class:`RuleFigures`, read by the caller from
``working_time_rule_set``. If a bucketing decision needed a figure the rule
set does not carry, that would be a finding for the decision register, not a
number written into this file — none turned out to be needed; see
``docs/DECISIONS.md`` for what WAS needed and how each field is used.

Three modelling choices below are not obvious, are not settled by any
published worked example, and are recorded here — and in the decision
register — precisely because reasonable people could bucket them differently.
This is deliberate rather than an oversight: there is no golden-file test for
hour bucketing (see ``calculators/tests/test_attendance.py``), so the
reasoning has to live in prose, not in a reproduced official answer.

**1. Sunday hours split into ordinary-Sunday and non-ordinary-Sunday
differently** (D-148). ``attendance_day`` has one ``sunday_hours`` column, not
two — the multiplier that applies to it is a *pay-time* decision read from
``work_schedule_day.is_working_day`` for that Sunday, not an *hour-bucketing*
one, EXCEPT for where the ordinary/overtime split itself happens. When Sunday
IS an ordinary working day for this employee, hours up to that day's ordinary
cap land in ``sunday_hours`` (paid at ``sunday_multiplier_ordinary``) and the
excess lands in ``overtime_hours`` — the same shape as any other ordinary day.
When Sunday is NOT ordinarily worked, BCEA s16(1) pays double time for the
whole day worked with no "ordinary Sunday allowance" to exceed, so every hour
worked lands in ``sunday_hours`` and none in ``overtime_hours`` — there is no
ordinary allocation on a day this employee does not ordinarily work, so
nothing on it can be "in excess of" one.

**2. Public holiday hours never split into overtime.** BCEA s18(2) pays a
worked public holiday at the full worked-multiplier for the whole day, not
only for hours beyond some cap — so every hour worked on a public holiday
lands in ``public_holiday_hours``, unlike Sunday's ordinary case.
``public_holiday_not_worked_paid`` is read and NOT used here: it decides
whether a day off on a public holiday is still paid a full day's wage, which
is a fact about total pay for a day with ZERO hours worked, not a fact about
any hour bucket — that decision belongs to payroll (P7), reading
``attendance_day.day_type`` directly.

**3. A standby day is its own path, not layered onto ordinary bucketing.**
``is_standby`` and ``day_type`` are independent columns, but this chunk models
a standby occasion as replacing the day's ordinary/Sunday/public-holiday split
rather than adding to it: ``standby_hours_worked`` is the actual work
performed, and only the excess over ``standby_hours_before_overtime`` becomes
``overtime_hours`` — the hours within the threshold are compensated by
``standby_allowance_per_shift`` (a flat rand figure, read and not used here
for the same reason as ``public_holiday_not_worked_paid``: it decides a
day's total pay, not an hour bucket). A person who works an ordinary shift
AND goes on standby afterwards on the SAME calendar day is out of scope for
this chunk and would need a second attendance_day-shaped row this table
does not yet have a place for — flagged in the decision register.

Both choices are BCEA/SD interpretations without a published worked example
behind them, and both are flagged for the labour law review (O-06) alongside
``days_worked_equivalent`` below.

**``night_hours`` is a proportional estimate, not an exact one.**
``attendance_day`` stores when a shift started and ended and how many minutes
of unpaid break it had, but not WHEN in the shift the break fell. This module
computes the raw overlap between the worked span and the night window, then
removes the break's share of it in proportion to how much of the raw span
overlapped the window. A shift entirely inside the window loses its whole
break from the night figure; a shift half inside loses half; this is an
estimate, not a reconstruction of an unrecorded fact, and is worth knowing
before this figure is relied on for the night allowance to the cent.

**``days_worked_equivalent`` is not obvious either, and is D-149.** The rule
chosen: 1.000 for ``leave``/``absent_paid`` (a paid day off still counts as a
full day for daily-rate pay and leave accrual — the person is being paid for
it); 0.000 for ``absent_unpaid``; and for every day with hours in it —
``ordinary``, ``rest_day``, ``sunday``, ``public_holiday``,
``no_work_available`` — the ratio of hours actually paid (worked hours plus
the SD1 guarantee) to the employee's own scheduled ordinary hours for that
day, capped at 1.000 so no single day is ever worth more than one. This is a
genuine choice: an equally defensible rule would count only ``ordinary_hours``
in the numerator, or would use the rule set's per-day cap instead of the
employee's own schedule as the denominator, and either would produce a
different number for a short day or a day with a non-standard schedule.
Flagged for the labour law review (O-06) rather than left implicit.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
_QUANTUM = Decimal("0.001")
_MINUTES_PER_HOUR = Decimal("60")
_MINUTES_PER_DAY = 24 * 60


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


class DayType(enum.StrEnum):
    """Mirrors ``attendance_day.day_type``'s values. Not imported from the
    model — this module may not import anything that imports Django.
    """

    ORDINARY = "ordinary"
    REST_DAY = "rest_day"
    SUNDAY = "sunday"
    PUBLIC_HOLIDAY = "public_holiday"
    LEAVE = "leave"
    ABSENT_UNPAID = "absent_unpaid"
    ABSENT_PAID = "absent_paid"
    NO_WORK_AVAILABLE = "no_work_available"


#: Day types where hours may be worked and bucketed. Every other day type
#: produces zero hours regardless of what time_in/time_out carry.
WORKED_DAY_TYPES = frozenset(
    {DayType.ORDINARY, DayType.REST_DAY, DayType.SUNDAY, DayType.PUBLIC_HOLIDAY}
)

#: Day types that still count as a full day for daily-rate pay and leave
#: accrual despite zero hours worked — see the days_worked_equivalent choice
#: (D-149) in the module docstring.
#:
#: HOLDS ONLY WHILE LEAVE IS WHOLE-DAY (D-149, amended). P6 introduces
#: leave_application with part days, and a half day of annual leave must
#: yield 0.500 here, not 1.000 — this flat lookup will need the part-day
#: fraction as an input once one exists. P6 must revisit this before
#: leave_application_id_ref becomes a real FK.
FULL_DAY_EQUIVALENT_TYPES = frozenset({DayType.LEAVE, DayType.ABSENT_PAID})


@dataclass(frozen=True)
class RuleFigures:
    """Every figure ``working_time_rule_set`` carries, as it applied on the
    work date. The caller reads the row through ``statutory.resolve`` — this
    module never resolves anything itself.
    """

    ordinary_hours_per_week: Decimal
    ordinary_hours_per_day_5day: Decimal
    ordinary_hours_per_day_6day: Decimal
    overtime_multiplier: Decimal
    max_overtime_hours_per_day: Decimal
    max_overtime_hours_per_week: Decimal
    sunday_multiplier_ordinary: Decimal
    sunday_multiplier_non_ordinary: Decimal
    public_holiday_worked_multiplier: Decimal
    public_holiday_not_worked_paid: bool
    night_work_start_time: datetime.time
    night_work_end_time: datetime.time
    night_allowance_type: str
    night_allowance_value: Decimal
    standby_allowance_per_shift: Decimal
    standby_window_start: datetime.time
    standby_window_end: datetime.time
    standby_hours_before_overtime: Decimal
    min_paid_hours_per_day: Decimal
    meal_interval_after_hours: Decimal
    meal_interval_minutes: int
    daily_rest_hours: int
    weekly_rest_hours: int

    def ordinary_cap_for(self, *, works_more_than_5_days_per_week: bool) -> Decimal:
        return (
            self.ordinary_hours_per_day_6day
            if works_more_than_5_days_per_week
            else self.ordinary_hours_per_day_5day
        )


@dataclass(frozen=True)
class AttendanceDayInput:
    """Facts about one employee on one work date. Assembled by the caller from
    ``attendance_day`` (as captured, before bucketing) and the employee's
    ``work_schedule``/``work_schedule_day`` for that weekday.
    """

    work_date: datetime.date
    day_type: str
    time_in: datetime.time | None
    time_out: datetime.time | None
    unpaid_break_minutes: int
    is_standby: bool
    #: From work_schedule_day.is_working_day for this weekday — NOT inferred
    #: from whether the employee happened to work. Decides which Sunday
    #: multiplier applies and whether a public holiday not worked is paid.
    is_ordinary_working_day: bool
    #: From work_schedule_day.ordinary_hours for this weekday, net of break —
    #: this employee's own agreed hours, not the statutory ceiling.
    scheduled_ordinary_hours: Decimal
    #: From work_schedule.days_per_week > 5 — selects which of the rule set's
    #: two per-day ordinary-hours columns is the statutory ceiling for today.
    works_more_than_5_days_per_week: bool = False


@dataclass(frozen=True)
class AttendanceDayResult:
    ordinary_hours: Decimal
    overtime_hours: Decimal
    sunday_hours: Decimal
    public_holiday_hours: Decimal
    night_hours: Decimal
    paid_hours_guaranteed: Decimal
    standby_hours_worked: Decimal
    days_worked_equivalent: Decimal
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _minutes_since_midnight(value: datetime.time) -> int:
    return value.hour * 60 + value.minute


def _span_minutes(start: datetime.time, end: datetime.time) -> int:
    """Minutes from start to end, wrapping past midnight if end is earlier.

    Equal start and end is zero minutes, not a full 24-hour day — the safer
    reading of a data error. A capture bug that leaves both fields at the same
    default value should produce an empty-looking day that is easy to spot,
    not a silently overpaid one.
    """
    a = _minutes_since_midnight(start)
    b = _minutes_since_midnight(end)
    if b < a:
        b += _MINUTES_PER_DAY
    return b - a


def _overlap_minutes(
    start_a: datetime.time, end_a: datetime.time, start_b: datetime.time, end_b: datetime.time
) -> int:
    """Overlap, in minutes, between two time-of-day intervals that may each
    wrap past midnight independently. Checks the second interval at its
    natural position and shifted a day either way, which is enough to catch
    every overlap between two spans that are each at most 24 hours.
    """
    a_start = _minutes_since_midnight(start_a)
    a_end = a_start + _span_minutes(start_a, end_a)
    b_start = _minutes_since_midnight(start_b)
    b_end = b_start + _span_minutes(start_b, end_b)

    total = 0
    for shift in (-_MINUTES_PER_DAY, 0, _MINUTES_PER_DAY):
        total += max(0, min(a_end, b_end + shift) - max(a_start, b_start + shift))
    return total


def _hours_worked(day: AttendanceDayInput) -> tuple[Decimal, tuple[str, ...]]:
    """Net hours worked, and any warnings about how they were derived."""
    if day.time_in is None or day.time_out is None:
        return ZERO, ()

    raw_minutes = _span_minutes(day.time_in, day.time_out)
    net_minutes = raw_minutes - day.unpaid_break_minutes
    if net_minutes < 0:
        return ZERO, (
            f"The {day.unpaid_break_minutes}-minute break is longer than the "
            f"{raw_minutes}-minute shift ({day.time_in}–{day.time_out}). Treated as "
            f"zero hours worked rather than a negative figure.",
        )
    return Decimal(net_minutes) / _MINUTES_PER_HOUR, ()


def _night_hours(day: AttendanceDayInput, rules: RuleFigures, hours_worked: Decimal) -> Decimal:
    """The worked span's overlap with the night window, net of a proportional
    share of the break. See the module docstring for why this is an estimate.
    """
    if day.time_in is None or day.time_out is None or hours_worked <= 0:
        return ZERO

    # raw_minutes cannot be <= 0 here: hours_worked > 0 already guarantees the
    # net (raw minus break) was positive, so the raw span was too.
    raw_minutes = _span_minutes(day.time_in, day.time_out)

    overlap = _overlap_minutes(
        day.time_in, day.time_out, rules.night_work_start_time, rules.night_work_end_time
    )
    if overlap <= 0:
        return ZERO

    fraction = Decimal(overlap) / Decimal(raw_minutes)
    return hours_worked * fraction


def bucket_day(day: AttendanceDayInput, rules: RuleFigures) -> AttendanceDayResult:
    """Bucket one day's hours. Stateless: the same inputs always produce the
    same result, on any date this function is called.
    """
    warnings: list[str] = []
    hours_worked, hour_warnings = _hours_worked(day)
    warnings.extend(hour_warnings)

    ordinary = overtime = sunday = public_holiday = ZERO
    standby_worked = ZERO

    if day.is_standby:
        standby_worked = hours_worked
        if standby_worked > rules.standby_hours_before_overtime:
            overtime = standby_worked - rules.standby_hours_before_overtime
    elif day.day_type in (DayType.ORDINARY, DayType.REST_DAY):
        cap = min(
            day.scheduled_ordinary_hours,
            rules.ordinary_cap_for(
                works_more_than_5_days_per_week=day.works_more_than_5_days_per_week
            ),
        )
        ordinary = min(hours_worked, cap)
        overtime = hours_worked - ordinary
    elif day.day_type == DayType.SUNDAY:
        if day.is_ordinary_working_day:
            cap = min(
                day.scheduled_ordinary_hours,
                rules.ordinary_cap_for(
                    works_more_than_5_days_per_week=day.works_more_than_5_days_per_week
                ),
            )
            sunday = min(hours_worked, cap)
            overtime = hours_worked - sunday
        else:
            sunday = hours_worked
    elif day.day_type == DayType.PUBLIC_HOLIDAY:
        public_holiday = hours_worked
    # LEAVE, ABSENT_PAID, ABSENT_UNPAID, NO_WORK_AVAILABLE: no hours worked,
    # whatever time_in/time_out carry is ignored — none of these are a day
    # this employee is expected to be at work at all.

    night = _night_hours(day, rules, hours_worked)

    worked_total = ordinary + overtime + sunday + public_holiday
    guarantee = ZERO
    if day.day_type == DayType.NO_WORK_AVAILABLE:
        guarantee = rules.min_paid_hours_per_day
    elif (
        day.day_type in WORKED_DAY_TYPES
        and worked_total > 0
        and worked_total < rules.min_paid_hours_per_day
    ):
        guarantee = rules.min_paid_hours_per_day - worked_total

    days_worked_equivalent = _days_worked_equivalent(
        day, rules, worked_total=worked_total, guarantee=guarantee
    )

    return AttendanceDayResult(
        ordinary_hours=_quantize(ordinary),
        overtime_hours=_quantize(overtime),
        sunday_hours=_quantize(sunday),
        public_holiday_hours=_quantize(public_holiday),
        night_hours=_quantize(night),
        paid_hours_guaranteed=_quantize(guarantee),
        standby_hours_worked=_quantize(standby_worked),
        days_worked_equivalent=_quantize(days_worked_equivalent),
        warnings=tuple(warnings),
    )


def _days_worked_equivalent(
    day: AttendanceDayInput, rules: RuleFigures, *, worked_total: Decimal, guarantee: Decimal
) -> Decimal:
    """D-149. See the module docstring for the rule and why it is a choice."""
    if day.day_type in FULL_DAY_EQUIVALENT_TYPES:
        return Decimal(1)
    if day.day_type == DayType.ABSENT_UNPAID:
        return ZERO

    paid_hours = worked_total + guarantee
    if paid_hours <= 0:
        return ZERO

    denominator = day.scheduled_ordinary_hours
    if denominator <= 0:
        denominator = rules.ordinary_cap_for(
            works_more_than_5_days_per_week=day.works_more_than_5_days_per_week
        )
    if denominator <= 0:
        return ZERO

    return min(Decimal(1), paid_hours / denominator)


# -------------------------------------------------------------- exceptions


class Severity(enum.StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True)
class AttendanceException:
    work_date: datetime.date
    severity: str
    message: str


@dataclass(frozen=True)
class SpanDay:
    """One day's input and its already-bucketed result, for exception review."""

    input: AttendanceDayInput
    result: AttendanceDayResult


def _week_key(a_date: datetime.date) -> tuple[int, int]:
    iso = a_date.isocalendar()
    return (iso[0], iso[1])


def evaluate_exceptions(
    days: tuple[SpanDay, ...], rules: RuleFigures
) -> tuple[AttendanceException, ...]:
    """The exceptions the capture screen shows live, over a span of days.

    Consecutive sick days needing a leave application are P6 — leave has no
    table to check against yet (``leave_application`` does not exist until
    P6), so that exception is not stubbed here; it is simply not produced.
    """
    ordered = sorted(days, key=lambda span: span.input.work_date)
    exceptions: list[AttendanceException] = []

    exceptions.extend(_daily_overtime_exceptions(ordered, rules))
    exceptions.extend(_weekly_overtime_exceptions(ordered, rules))
    exceptions.extend(_daily_ceiling_exceptions(ordered, rules))
    exceptions.extend(_daily_rest_exceptions(ordered, rules))
    exceptions.extend(_weekly_rest_exceptions(ordered, rules))
    exceptions.extend(_meal_interval_exceptions(ordered, rules))

    return tuple(exceptions)


def _daily_overtime_exceptions(ordered, rules: RuleFigures) -> list[AttendanceException]:
    found = []
    for span in ordered:
        overtime = span.result.overtime_hours
        if overtime > rules.max_overtime_hours_per_day:
            found.append(
                AttendanceException(
                    work_date=span.input.work_date,
                    severity=Severity.BLOCKING,
                    message=(
                        f"{overtime} hours' overtime exceeds the "
                        f"{rules.max_overtime_hours_per_day}-hour daily maximum "
                        f"(working_time_rule_set.max_overtime_hours_per_day)."
                    ),
                )
            )
    return found


def _weekly_overtime_exceptions(ordered, rules: RuleFigures) -> list[AttendanceException]:
    totals: dict[tuple[int, int], Decimal] = {}
    last_date: dict[tuple[int, int], datetime.date] = {}
    for span in ordered:
        key = _week_key(span.input.work_date)
        totals[key] = totals.get(key, ZERO) + span.result.overtime_hours
        last_date[key] = span.input.work_date

    found = []
    for key, total in totals.items():
        if total > rules.max_overtime_hours_per_week:
            found.append(
                AttendanceException(
                    work_date=last_date[key],
                    severity=Severity.BLOCKING,
                    message=(
                        f"{total} hours' overtime in ISO week {key[1]} of {key[0]} exceeds "
                        f"the {rules.max_overtime_hours_per_week}-hour weekly maximum "
                        f"(working_time_rule_set.max_overtime_hours_per_week)."
                    ),
                )
            )
    return found


def _daily_ceiling_exceptions(ordered, rules: RuleFigures) -> list[AttendanceException]:
    found = []
    for span in ordered:
        cap = rules.ordinary_cap_for(
            works_more_than_5_days_per_week=span.input.works_more_than_5_days_per_week
        )
        ceiling = cap + rules.max_overtime_hours_per_day
        total = span.result.ordinary_hours + span.result.overtime_hours
        if total > ceiling:
            found.append(
                AttendanceException(
                    work_date=span.input.work_date,
                    severity=Severity.BLOCKING,
                    message=(
                        f"{total} ordinary-plus-overtime hours exceeds the {ceiling}-hour "
                        f"daily ceiling (working_time_rule_set ordinary cap plus "
                        f"max_overtime_hours_per_day)."
                    ),
                )
            )
    return found


def _shift_bounds_minutes(span: SpanDay) -> tuple[int, int] | None:
    """This day's shift as (start, end) minutes on a timeline anchored at its
    own work_date's midnight, or None if it has no recorded shift.
    """
    day = span.input
    if day.time_in is None or day.time_out is None:
        return None
    start = _minutes_since_midnight(day.time_in)
    end = start + _span_minutes(day.time_in, day.time_out)
    return start, end


def _daily_rest_exceptions(ordered, rules: RuleFigures) -> list[AttendanceException]:
    found = []
    required_minutes = rules.daily_rest_hours * 60
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if (later.input.work_date - earlier.input.work_date).days != 1:
            continue
        earlier_bounds = _shift_bounds_minutes(earlier)
        later_bounds = _shift_bounds_minutes(later)
        if earlier_bounds is None or later_bounds is None:
            continue

        _, earlier_end = earlier_bounds
        later_start, _ = later_bounds
        rest_minutes = (later_start + _MINUTES_PER_DAY) - earlier_end
        if rest_minutes < required_minutes:
            found.append(
                AttendanceException(
                    work_date=later.input.work_date,
                    severity=Severity.WARNING,
                    message=(
                        f"Only {Decimal(rest_minutes) / _MINUTES_PER_HOUR} hours' rest "
                        f"before this shift, under the {rules.daily_rest_hours}-hour "
                        f"minimum (working_time_rule_set.daily_rest_hours)."
                    ),
                )
            )
    return found


def _weekly_rest_exceptions(ordered, rules: RuleFigures) -> list[AttendanceException]:
    """Flags a week with no single rest gap meeting the weekly minimum.

    Gaps are computed between EVERY pair of consecutive rows with a recorded
    shift, across the whole span — not only between rows that fall in the
    same ISO week. The real weekly rest period usually falls on a day with no
    row at all (nobody works Sundays, so there is no ``attendance_day`` row
    for one), and that day sits *between* the two rows the gap is measured
    from; restricting the search to one week's own rows would never see it. A
    gap is attributed to the ISO week of its EARLIER day — "did this week's
    work end with adequate rest" — so a week with no shift recorded at all
    contributes no gap and is silently skipped, and so is the final week in
    a span, which has no later shift yet to measure a closing gap against.
    """
    required_minutes = rules.weekly_rest_hours * 60
    gaps_by_week: dict[tuple[int, int], list[int]] = {}

    for earlier, later in zip(ordered, ordered[1:], strict=False):
        earlier_bounds = _shift_bounds_minutes(earlier)
        later_bounds = _shift_bounds_minutes(later)
        if earlier_bounds is None or later_bounds is None:
            continue
        day_gap = (later.input.work_date - earlier.input.work_date).days
        _, earlier_end = earlier_bounds
        later_start, _ = later_bounds
        rest_minutes = (later_start + day_gap * _MINUTES_PER_DAY) - earlier_end
        gaps_by_week.setdefault(_week_key(earlier.input.work_date), []).append(rest_minutes)

    found = []
    for key, gaps in gaps_by_week.items():
        longest = max(gaps)
        if longest < required_minutes:
            found.append(
                AttendanceException(
                    work_date=max(
                        span.input.work_date
                        for span in ordered
                        if _week_key(span.input.work_date) == key
                    ),
                    severity=Severity.WARNING,
                    message=(
                        f"No rest period starting in ISO week {key[1]} of {key[0]} reaches "
                        f"the {rules.weekly_rest_hours}-hour weekly minimum "
                        f"(working_time_rule_set.weekly_rest_hours); the longest was "
                        f"{Decimal(longest) / _MINUTES_PER_HOUR} hours."
                    ),
                )
            )
    return found


def _meal_interval_exceptions(ordered, rules: RuleFigures) -> list[AttendanceException]:
    found = []
    for span in ordered:
        day = span.input
        if day.time_in is None or day.time_out is None:
            continue
        raw_hours = Decimal(_span_minutes(day.time_in, day.time_out)) / _MINUTES_PER_HOUR
        if raw_hours <= rules.meal_interval_after_hours:
            continue
        if day.unpaid_break_minutes >= rules.meal_interval_minutes:
            continue
        found.append(
            AttendanceException(
                work_date=day.work_date,
                severity=Severity.WARNING,
                message=(
                    f"A {raw_hours}-hour shift exceeds the "
                    f"{rules.meal_interval_after_hours}-hour meal-interval trigger but "
                    f"only {day.unpaid_break_minutes} minutes of break are recorded, "
                    f"under the {rules.meal_interval_minutes}-minute minimum "
                    f"(working_time_rule_set.meal_interval_after_hours / "
                    f"meal_interval_minutes)."
                ),
            )
        )
    return found
