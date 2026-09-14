"""Hour bucketing — table-driven boundaries and property-based invariants.

There is NO published worked example for hour bucketing (see the decision
register), so there is no golden file here. This is the substitute: every
bucket, every boundary the task named explicitly, and the invariants that
must hold no matter what a capture screen sends in.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from calculators.attendance import (
    AttendanceDayInput,
    DayType,
    RuleFigures,
    Severity,
    SpanDay,
    bucket_day,
    evaluate_exceptions,
)

D = Decimal


def make_rules(**overrides) -> RuleFigures:
    values = {
        "ordinary_hours_per_week": D("45"),
        "ordinary_hours_per_day_5day": D("9"),
        "ordinary_hours_per_day_6day": D("8"),
        "overtime_multiplier": D("1.5"),
        "max_overtime_hours_per_day": D("3"),
        "max_overtime_hours_per_week": D("10"),
        "sunday_multiplier_ordinary": D("1.5"),
        "sunday_multiplier_non_ordinary": D("2.0"),
        "public_holiday_worked_multiplier": D("2.0"),
        "public_holiday_not_worked_paid": True,
        "night_work_start_time": datetime.time(18, 0),
        "night_work_end_time": datetime.time(6, 0),
        "night_allowance_type": "percentage",
        "night_allowance_value": D("10"),
        "standby_allowance_per_shift": D("50.00"),
        "standby_window_start": datetime.time(18, 0),
        "standby_window_end": datetime.time(6, 0),
        "standby_hours_before_overtime": D("2"),
        "min_paid_hours_per_day": D("6"),
        "meal_interval_after_hours": D("5"),
        "meal_interval_minutes": 60,
        "daily_rest_hours": 12,
        "weekly_rest_hours": 36,
    }
    values.update(overrides)
    return RuleFigures(**values)


def make_day(**overrides) -> AttendanceDayInput:
    values = {
        "work_date": datetime.date(2026, 3, 2),  # a Monday
        "day_type": DayType.ORDINARY,
        "time_in": datetime.time(8, 0),
        "time_out": datetime.time(17, 0),
        "unpaid_break_minutes": 60,
        "is_standby": False,
        "is_ordinary_working_day": True,
        "scheduled_ordinary_hours": D("8"),
        "works_more_than_5_days_per_week": False,
    }
    values.update(overrides)
    return AttendanceDayInput(**values)


# ------------------------------------------------------------ table-driven


def test_exactly_ordinary_hours_produces_no_overtime():
    result = bucket_day(
        make_day(time_in=datetime.time(8, 0), time_out=datetime.time(17, 0)), make_rules()
    )

    assert result.ordinary_hours == D("8.000")
    assert result.overtime_hours == D("0.000")


def test_one_minute_over_ordinary_is_overtime():
    result = bucket_day(
        make_day(time_in=datetime.time(8, 0), time_out=datetime.time(17, 1)), make_rules()
    )

    assert result.ordinary_hours == D("8.000")
    assert result.overtime_hours == D("0.017")


def test_a_shift_crossing_the_night_window_at_both_ends():
    """16:00-08:00 wholly contains the 18:00-06:00 night window, with two
    hours before it opens and two after it closes.
    """
    day = make_day(
        time_in=datetime.time(16, 0),
        time_out=datetime.time(8, 0),
        unpaid_break_minutes=0,
        scheduled_ordinary_hours=D("16"),
    )
    result = bucket_day(day, make_rules())

    assert result.night_hours == D("12.000")


def test_a_shift_entirely_outside_the_night_window_has_no_night_hours():
    result = bucket_day(
        make_day(time_in=datetime.time(8, 0), time_out=datetime.time(16, 0)), make_rules()
    )

    assert result.night_hours == D("0.000")


def test_sunday_for_someone_who_ordinarily_works_sundays_splits_overtime():
    day = make_day(
        day_type=DayType.SUNDAY,
        is_ordinary_working_day=True,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(19, 0),
        unpaid_break_minutes=60,
        scheduled_ordinary_hours=D("8"),
    )
    result = bucket_day(day, make_rules())

    assert result.sunday_hours == D("8.000")
    assert result.overtime_hours == D("2.000")


def test_sunday_for_someone_who_does_not_ordinarily_work_sundays_is_all_sunday_hours():
    day = make_day(
        day_type=DayType.SUNDAY,
        is_ordinary_working_day=False,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(19, 0),
        unpaid_break_minutes=60,
    )
    result = bucket_day(day, make_rules())

    assert result.sunday_hours == D("10.000")
    assert result.overtime_hours == D("0.000")


def test_a_public_holiday_worked_is_all_public_holiday_hours():
    day = make_day(
        day_type=DayType.PUBLIC_HOLIDAY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(19, 0),
        unpaid_break_minutes=60,
    )
    result = bucket_day(day, make_rules())

    assert result.public_holiday_hours == D("10.000")
    assert result.overtime_hours == D("0.000")


def test_a_public_holiday_not_worked_produces_no_hours():
    day = make_day(
        day_type=DayType.PUBLIC_HOLIDAY, time_in=None, time_out=None, unpaid_break_minutes=0
    )
    result = bucket_day(day, make_rules())

    assert result.public_holiday_hours == D("0.000")
    assert result.ordinary_hours == D("0.000")
    assert result.paid_hours_guaranteed == D("0.000")


def test_a_short_day_below_the_guarantee_is_topped_up():
    day = make_day(
        time_in=datetime.time(8, 0), time_out=datetime.time(11, 0), unpaid_break_minutes=0
    )
    result = bucket_day(day, make_rules())  # 3 hours worked, guarantee is 6

    assert result.ordinary_hours == D("3.000")
    assert result.paid_hours_guaranteed == D("3.000")


def test_no_work_available_pays_the_full_guarantee():
    day = make_day(day_type=DayType.NO_WORK_AVAILABLE, time_in=None, time_out=None)
    result = bucket_day(day, make_rules())

    assert result.paid_hours_guaranteed == D("6.000")


def test_a_standby_shift_with_work_beyond_the_threshold_is_overtime():
    day = make_day(
        is_standby=True,
        time_in=datetime.time(22, 0),
        time_out=datetime.time(1, 0),
        unpaid_break_minutes=0,
    )
    result = bucket_day(day, make_rules())  # 3 hours worked, threshold is 2

    assert result.standby_hours_worked == D("3.000")
    assert result.overtime_hours == D("1.000")


def test_a_standby_shift_with_no_work_performed():
    day = make_day(is_standby=True, time_in=None, time_out=None)
    result = bucket_day(day, make_rules())

    assert result.standby_hours_worked == D("0.000")
    assert result.overtime_hours == D("0.000")
    assert result.paid_hours_guaranteed == D("0.000")


# ------------------------------------------------- bare hours worked (D-157)


def test_a_bare_hours_total_still_buckets_standby_correctly():
    """Standby pay does not depend on clock position at all — bucket_day's
    is_standby branch reads only is_standby and the worked total, never
    time_in/time_out or the standby window. A bare total is exactly as good
    as a time span for this figure.
    """
    day = make_day(is_standby=True, time_in=None, time_out=None, hours_worked=D("3"))
    result = bucket_day(day, make_rules())

    assert result.standby_hours_worked == D("3.000")
    assert result.overtime_hours == D("1.000")  # threshold is 2


def test_a_bare_hours_standby_day_warns_that_night_hours_cannot_be_estimated():
    day = make_day(is_standby=True, time_in=None, time_out=None, hours_worked=D("3"))
    result = bucket_day(day, make_rules())

    assert result.night_hours == D("0.000")
    assert len(result.warnings) == 1
    assert "standby occasion" in result.warnings[0]
    assert "night" in result.warnings[0].lower()


def test_a_bare_hours_day_on_a_schedule_crossing_the_night_window_warns():
    day = make_day(
        time_in=None,
        time_out=None,
        hours_worked=D("8"),
        scheduled_start_time=datetime.time(22, 0),
        scheduled_end_time=datetime.time(6, 0),
    )
    result = bucket_day(day, make_rules())

    assert result.night_hours == D("0.000")
    assert len(result.warnings) == 1
    assert "crosses the night window" in result.warnings[0]


def test_a_bare_hours_day_on_a_daytime_schedule_has_no_warning():
    """The simple form stays simple for the ordinary household case: an
    eight-to-five schedule never touches the night window.
    """
    day = make_day(
        time_in=None,
        time_out=None,
        hours_worked=D("8"),
        scheduled_start_time=datetime.time(8, 0),
        scheduled_end_time=datetime.time(17, 0),
    )
    result = bucket_day(day, make_rules())

    assert result.warnings == ()


def test_a_bare_hours_day_with_no_schedule_times_on_file_has_no_warning():
    """No schedule span to test against means no basis to warn — silence
    here is genuine absence of a signal, not a claim that nothing is wrong.
    """
    day = make_day(
        time_in=None,
        time_out=None,
        hours_worked=D("8"),
        scheduled_start_time=None,
        scheduled_end_time=None,
    )
    result = bucket_day(day, make_rules())

    assert result.warnings == ()


def test_a_captured_time_span_never_warns_even_on_a_night_schedule():
    """A time span always wins — the warning exists only for the ambiguity a
    bare total creates, and a real time span has none.
    """
    day = make_day(
        time_in=datetime.time(22, 0),
        time_out=datetime.time(6, 0),
        unpaid_break_minutes=0,
        scheduled_start_time=datetime.time(22, 0),
        scheduled_end_time=datetime.time(6, 0),
    )
    result = bucket_day(day, make_rules())

    assert result.warnings == ()


def test_leave_counts_as_a_full_day_equivalent_with_no_hours():
    day = make_day(day_type=DayType.LEAVE, time_in=None, time_out=None)
    result = bucket_day(day, make_rules())

    assert result.days_worked_equivalent == D("1.000")
    assert result.ordinary_hours == D("0.000")


def test_absent_paid_counts_as_a_full_day_equivalent():
    result = bucket_day(
        make_day(day_type=DayType.ABSENT_PAID, time_in=None, time_out=None), make_rules()
    )

    assert result.days_worked_equivalent == D("1.000")


def test_absent_unpaid_counts_as_no_day_at_all():
    result = bucket_day(
        make_day(day_type=DayType.ABSENT_UNPAID, time_in=None, time_out=None), make_rules()
    )

    assert result.days_worked_equivalent == D("0.000")


def test_a_full_day_worked_is_exactly_one_days_worked_equivalent():
    result = bucket_day(make_day(), make_rules())

    assert result.days_worked_equivalent == D("1.000")


def test_a_half_day_is_half_a_days_worked_equivalent():
    day = make_day(
        time_in=datetime.time(8, 0), time_out=datetime.time(12, 30), unpaid_break_minutes=0
    )
    result = bucket_day(day, make_rules())  # 4.5 hours worked, topped up to 6 by the guarantee

    assert result.days_worked_equivalent == D("0.750")  # 6 guaranteed hours / 8 scheduled


def test_rest_day_worked_splits_into_ordinary_and_overtime_like_any_other_day():
    day = make_day(
        day_type=DayType.REST_DAY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(19, 0),
        unpaid_break_minutes=60,
    )
    result = bucket_day(day, make_rules())

    assert result.ordinary_hours == D("8.000")
    assert result.overtime_hours == D("2.000")


def test_a_break_longer_than_the_shift_is_zero_hours_with_a_warning():
    day = make_day(
        time_in=datetime.time(8, 0), time_out=datetime.time(9, 0), unpaid_break_minutes=90
    )
    result = bucket_day(day, make_rules())

    assert result.ordinary_hours == D("0.000")
    assert result.warnings
    assert "longer than" in result.warnings[0]


def test_no_scheduled_hours_falls_back_to_the_rule_sets_daily_cap():
    """A day worked with no schedule entry (an ad-hoc call-in) falls back to
    the statutory per-day cap as the days-worked-equivalent denominator.
    """
    day = make_day(scheduled_ordinary_hours=D("0"), works_more_than_5_days_per_week=False)
    result = bucket_day(day, make_rules())

    # 8 hours worked / 9-hour 5-day statutory cap
    assert result.days_worked_equivalent == D("0.889")


def test_no_schedule_and_no_rule_set_cap_is_zero_equivalent():
    day = make_day(scheduled_ordinary_hours=D("0"))
    rules = make_rules(ordinary_hours_per_day_5day=D("0"), ordinary_hours_per_day_6day=D("0"))
    result = bucket_day(day, rules)

    assert result.days_worked_equivalent == D("0.000")


def test_six_day_week_uses_the_six_day_ordinary_cap():
    day = make_day(
        works_more_than_5_days_per_week=True,
        scheduled_ordinary_hours=D("8"),
        time_in=datetime.time(8, 0),
        time_out=datetime.time(16, 30),
        unpaid_break_minutes=30,
    )
    result = bucket_day(day, make_rules())

    assert result.ordinary_hours == D("8.000")
    assert result.overtime_hours == D("0.000")


# --------------------------------------------------------- property-based


day_type_strategy = st.sampled_from(list(DayType))
time_strategy = st.times()
break_strategy = st.integers(min_value=0, max_value=120)
hours_strategy = st.decimals(min_value="0", max_value="12", places=2, allow_nan=False)


@given(
    day_type=day_type_strategy,
    time_in=st.one_of(st.none(), time_strategy),
    time_out=st.one_of(st.none(), time_strategy),
    unpaid_break_minutes=break_strategy,
    is_standby=st.booleans(),
    is_ordinary_working_day=st.booleans(),
    scheduled_ordinary_hours=hours_strategy,
    works_more_than_5_days_per_week=st.booleans(),
)
@settings(max_examples=200)
def test_no_bucket_is_ever_negative(
    day_type,
    time_in,
    time_out,
    unpaid_break_minutes,
    is_standby,
    is_ordinary_working_day,
    scheduled_ordinary_hours,
    works_more_than_5_days_per_week,
):
    day = AttendanceDayInput(
        work_date=datetime.date(2026, 3, 2),
        day_type=day_type,
        time_in=time_in,
        time_out=time_out,
        unpaid_break_minutes=unpaid_break_minutes,
        is_standby=is_standby,
        is_ordinary_working_day=is_ordinary_working_day,
        scheduled_ordinary_hours=scheduled_ordinary_hours,
        works_more_than_5_days_per_week=works_more_than_5_days_per_week,
    )
    result = bucket_day(day, make_rules())

    for value in (
        result.ordinary_hours,
        result.overtime_hours,
        result.sunday_hours,
        result.public_holiday_hours,
        result.night_hours,
        result.paid_hours_guaranteed,
        result.standby_hours_worked,
        result.days_worked_equivalent,
    ):
        assert value >= 0


@given(
    day_type=day_type_strategy,
    time_in=time_strategy,
    time_out=time_strategy,
    unpaid_break_minutes=break_strategy,
    is_standby=st.booleans(),
    is_ordinary_working_day=st.booleans(),
    scheduled_ordinary_hours=hours_strategy,
)
@settings(max_examples=200)
def test_the_worked_buckets_never_exceed_24_hours(
    day_type,
    time_in,
    time_out,
    unpaid_break_minutes,
    is_standby,
    is_ordinary_working_day,
    scheduled_ordinary_hours,
):
    day = AttendanceDayInput(
        work_date=datetime.date(2026, 3, 2),
        day_type=day_type,
        time_in=time_in,
        time_out=time_out,
        unpaid_break_minutes=unpaid_break_minutes,
        is_standby=is_standby,
        is_ordinary_working_day=is_ordinary_working_day,
        scheduled_ordinary_hours=scheduled_ordinary_hours,
    )
    result = bucket_day(day, make_rules())

    total = (
        result.ordinary_hours
        + result.overtime_hours
        + result.sunday_hours
        + result.public_holiday_hours
    )
    assert total <= D("24.000")


@given(
    scheduled_ordinary_hours=hours_strategy,
    time_in=time_strategy,
    time_out=time_strategy,
    works_more_than_5_days_per_week=st.booleans(),
)
@settings(max_examples=200)
def test_ordinary_never_exceeds_the_schedules_ordinary_hours(
    scheduled_ordinary_hours, time_in, time_out, works_more_than_5_days_per_week
):
    day = make_day(
        time_in=time_in,
        time_out=time_out,
        unpaid_break_minutes=0,
        scheduled_ordinary_hours=scheduled_ordinary_hours,
        works_more_than_5_days_per_week=works_more_than_5_days_per_week,
    )
    result = bucket_day(day, make_rules())

    assert result.ordinary_hours <= scheduled_ordinary_hours


@given(time_in=time_strategy, time_out=time_strategy, scheduled_ordinary_hours=hours_strategy)
@settings(max_examples=200)
def test_overtime_is_exactly_the_excess_over_ordinary(time_in, time_out, scheduled_ordinary_hours):
    """For an ordinary day, overtime is whatever ordinary did not absorb —
    there is no third place the worked hours could have gone.
    """
    day = make_day(
        time_in=time_in,
        time_out=time_out,
        unpaid_break_minutes=0,
        scheduled_ordinary_hours=scheduled_ordinary_hours,
    )
    result = bucket_day(day, make_rules())

    raw_minutes = time_out.hour * 60 + time_out.minute - (time_in.hour * 60 + time_in.minute)
    if raw_minutes < 0:
        raw_minutes += 24 * 60
    hours_worked = (Decimal(raw_minutes) / Decimal(60)).quantize(Decimal("0.001"))

    assert abs((result.ordinary_hours + result.overtime_hours) - hours_worked) <= Decimal("0.001")


@pytest.mark.parametrize(
    "day_type",
    [DayType.LEAVE, DayType.ABSENT_UNPAID, DayType.ABSENT_PAID],
)
def test_a_day_with_no_work_produces_no_paid_hours_except_the_guarantee(day_type):
    day = make_day(day_type=day_type, time_in=None, time_out=None)
    result = bucket_day(day, make_rules())

    assert result.ordinary_hours == 0
    assert result.overtime_hours == 0
    assert result.sunday_hours == 0
    assert result.public_holiday_hours == 0
    assert result.paid_hours_guaranteed == 0


# --------------------------------------------------------------- exceptions


def make_span(**overrides) -> SpanDay:
    day = make_day(
        **{k: v for k, v in overrides.items() if k in AttendanceDayInput.__dataclass_fields__}
    )
    result = bucket_day(day, make_rules())
    return SpanDay(input=day, result=result)


def test_daily_overtime_over_the_maximum_is_blocking():
    day = make_day(
        time_in=datetime.time(6, 0), time_out=datetime.time(18, 0), unpaid_break_minutes=0
    )
    result = bucket_day(day, make_rules())
    span = SpanDay(input=day, result=result)

    exceptions = evaluate_exceptions((span,), make_rules())

    overtime_exceptions = [e for e in exceptions if "daily maximum" in e.message]
    assert overtime_exceptions
    assert overtime_exceptions[0].severity == Severity.BLOCKING
    assert overtime_exceptions[0].work_date == day.work_date


def test_no_daily_overtime_exception_when_within_the_maximum():
    span = make_span()
    exceptions = evaluate_exceptions((span,), make_rules())

    assert not [e for e in exceptions if "daily maximum" in e.message]


def test_weekly_overtime_over_the_maximum_is_blocking():
    rules = make_rules()
    spans = tuple(
        make_span(
            work_date=datetime.date(2026, 3, 2) + datetime.timedelta(days=i),
            time_in=datetime.time(6, 0),
            time_out=datetime.time(17, 0),
            unpaid_break_minutes=0,
        )
        for i in range(5)
    )

    exceptions = evaluate_exceptions(spans, rules)

    weekly = [e for e in exceptions if "weekly maximum" in e.message]
    assert weekly
    assert weekly[0].severity == Severity.BLOCKING


def test_the_combined_daily_ceiling_is_blocking():
    rules = make_rules()  # ordinary cap 9 + max_overtime_hours_per_day 3 = 12
    day = make_day(
        time_in=datetime.time(0, 0), time_out=datetime.time(15, 0), unpaid_break_minutes=0
    )
    result = bucket_day(day, rules)
    span = SpanDay(input=day, result=result)

    exceptions = evaluate_exceptions((span,), rules)

    ceiling = [e for e in exceptions if "daily ceiling" in e.message]
    assert ceiling
    assert ceiling[0].severity == Severity.BLOCKING


def test_daily_rest_under_the_minimum_between_consecutive_days_is_a_warning():
    rules = make_rules(daily_rest_hours=12)
    day_one = make_day(
        work_date=datetime.date(2026, 3, 2),
        time_in=datetime.time(8, 0),
        time_out=datetime.time(22, 0),
        unpaid_break_minutes=0,
    )
    day_two = make_day(
        work_date=datetime.date(2026, 3, 3),
        time_in=datetime.time(6, 0),
        time_out=datetime.time(14, 0),
        unpaid_break_minutes=0,
    )
    spans = (
        SpanDay(input=day_one, result=bucket_day(day_one, rules)),
        SpanDay(input=day_two, result=bucket_day(day_two, rules)),
    )

    exceptions = evaluate_exceptions(spans, rules)

    rest = [e for e in exceptions if "rest before this shift" in e.message]
    assert rest
    assert rest[0].severity == Severity.WARNING
    assert rest[0].work_date == day_two.work_date


def test_daily_rest_is_not_checked_across_a_gap_in_dates():
    day_one = make_day(work_date=datetime.date(2026, 3, 2))
    day_three = make_day(work_date=datetime.date(2026, 3, 4))
    spans = (
        SpanDay(input=day_one, result=bucket_day(day_one, make_rules())),
        SpanDay(input=day_three, result=bucket_day(day_three, make_rules())),
    )

    exceptions = evaluate_exceptions(spans, make_rules())

    assert not [e for e in exceptions if "rest before this shift" in e.message]


def test_daily_rest_is_skipped_when_a_day_has_no_recorded_shift():
    day_one = make_day(work_date=datetime.date(2026, 3, 2))
    day_two = make_day(
        work_date=datetime.date(2026, 3, 3),
        day_type=DayType.ABSENT_UNPAID,
        time_in=None,
        time_out=None,
    )
    spans = (
        SpanDay(input=day_one, result=bucket_day(day_one, make_rules())),
        SpanDay(input=day_two, result=bucket_day(day_two, make_rules())),
    )

    exceptions = evaluate_exceptions(spans, make_rules())

    assert not [e for e in exceptions if "rest before this shift" in e.message]


def test_weekly_rest_under_the_minimum_is_a_warning():
    rules = make_rules(weekly_rest_hours=36)
    spans = tuple(
        make_span(
            work_date=datetime.date(2026, 3, 2) + datetime.timedelta(days=i),
            time_in=datetime.time(6, 0),
            time_out=datetime.time(20, 0),
            unpaid_break_minutes=0,
        )
        for i in range(7)
    )

    exceptions = evaluate_exceptions(spans, rules)

    weekly_rest = [e for e in exceptions if "weekly minimum" in e.message]
    assert weekly_rest
    assert weekly_rest[0].severity == Severity.WARNING


def test_weekly_rest_is_fine_with_a_proper_rest_day():
    """Monday to Saturday worked, Sunday left with no row at all, then the
    following Monday worked again — the real rest period straddles a day
    with no shift recorded, which is the ordinary case for a rest day.
    """
    rules = make_rules(weekly_rest_hours=36)
    monday_to_saturday = [
        make_span(
            work_date=datetime.date(2026, 3, 2) + datetime.timedelta(days=i),
            time_in=datetime.time(8, 0),
            time_out=datetime.time(17, 0),
            unpaid_break_minutes=60,
        )
        for i in range(6)
    ]
    following_monday = make_span(
        work_date=datetime.date(2026, 3, 9),
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    spans = tuple(monday_to_saturday) + (following_monday,)

    exceptions = evaluate_exceptions(spans, rules)

    assert not [e for e in exceptions if "weekly minimum" in e.message]


def test_weekly_rest_is_skipped_for_a_week_with_only_one_shift():
    span = make_span()
    exceptions = evaluate_exceptions((span,), make_rules())

    assert not [e for e in exceptions if "weekly minimum" in e.message]


def test_a_missing_meal_interval_is_a_warning():
    rules = make_rules(meal_interval_after_hours=D("5"), meal_interval_minutes=60)
    day = make_day(
        time_in=datetime.time(7, 0), time_out=datetime.time(15, 0), unpaid_break_minutes=15
    )
    span = SpanDay(input=day, result=bucket_day(day, rules))

    exceptions = evaluate_exceptions((span,), rules)

    meal = [e for e in exceptions if "meal-interval" in e.message]
    assert meal
    assert meal[0].severity == Severity.WARNING


def test_a_long_shift_with_a_proper_meal_break_has_no_exception():
    rules = make_rules(meal_interval_after_hours=D("5"), meal_interval_minutes=60)
    day = make_day(
        time_in=datetime.time(7, 0), time_out=datetime.time(16, 0), unpaid_break_minutes=60
    )
    span = SpanDay(input=day, result=bucket_day(day, rules))

    exceptions = evaluate_exceptions((span,), rules)

    assert not [e for e in exceptions if "meal-interval" in e.message]


def test_a_short_shift_never_triggers_the_meal_interval_check():
    rules = make_rules(meal_interval_after_hours=D("5"), meal_interval_minutes=60)
    day = make_day(
        time_in=datetime.time(8, 0), time_out=datetime.time(11, 0), unpaid_break_minutes=0
    )
    span = SpanDay(input=day, result=bucket_day(day, rules))

    exceptions = evaluate_exceptions((span,), rules)

    assert not [e for e in exceptions if "meal-interval" in e.message]


def test_meal_interval_is_skipped_for_a_day_with_no_recorded_shift():
    day = make_day(day_type=DayType.ABSENT_UNPAID, time_in=None, time_out=None)
    span = SpanDay(input=day, result=bucket_day(day, make_rules()))

    exceptions = evaluate_exceptions((span,), make_rules())

    assert not [e for e in exceptions if "meal-interval" in e.message]


def test_exceptions_are_returned_in_work_date_order():
    rules = make_rules(max_overtime_hours_per_day=D("0"))
    spans = tuple(
        make_span(
            work_date=datetime.date(2026, 3, 2) + datetime.timedelta(days=i),
            time_in=datetime.time(6, 0),
            time_out=datetime.time(18, 0),
            unpaid_break_minutes=0,
        )
        for i in reversed(range(3))
    )

    exceptions = evaluate_exceptions(spans, rules)
    dates = [e.work_date for e in exceptions if "daily maximum" in e.message]

    assert dates == sorted(dates)
