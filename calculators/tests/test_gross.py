"""Gross pay: the branches the golden file does not reach.

The golden file holds this calculator to the Act's own words. This one holds it
to the edges — the two refusals, the five bases' basic pay, the unpaid absence,
and one run of the real ``bucket_day()`` straight into it, so the two
calculators are proven to fit rather than assumed to.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from calculators.attendance import AttendanceDayInput, DayType, RuleFigures, bucket_day
from calculators.gross import (
    ATTENDANCE_DRIVEN,
    GrossInput,
    GrossPayRefusedError,
    NightAllowanceKind,
    PayBasis,
    PremiumRates,
    gross_pay,
)
from calculators.tests.test_gross_statute import (
    BCEA_RATES,
    DAILY,
    HOLIDAY,
    HOURLY,
    MARCH,
    SHIFT,
    SUNDAY,
    a_day,
    amount_of,
    an_input,
)

MONTHLY_SALARY = Decimal("8775.00")
WORKING_DAYS = Decimal("21")


def salaried(**overrides) -> GrossInput:
    values = {
        "pay_basis": PayBasis.MONTHLY,
        "period_rate": MONTHLY_SALARY,
        "working_days_in_period": WORKING_DAYS,
    }
    values.update(overrides)
    return an_input(**values)


# ------------------------------------------------------------ the five bases


def test_the_two_attendance_driven_bases_are_named_once():
    assert ATTENDANCE_DRIVEN == {PayBasis.HOURLY, PayBasis.DAILY}
    assert len(PayBasis) == 5


def test_an_hourly_basic_is_the_ordinary_hours_and_the_short_day_guarantee():
    """SD1 guarantees paid hours to an employee called in for a short day, and
    ``bucket_day()`` reports the shortfall separately. It is paid at the
    ordinary rate, so it belongs in BASIC and not in a premium."""
    result = gross_pay(
        an_input(
            days=(
                a_day(
                    ordinary=Decimal("3.00"),
                    guarantee=Decimal("3.00"),
                    days_equivalent=Decimal("0.667"),
                ),
            )
        )
    )

    assert amount_of(result, "BASIC") == Decimal("6.00") * HOURLY


def test_a_daily_basic_is_the_days_worked_equivalent():
    """A short day is a fraction of a day's pay on a daily basis — D-149's own
    column, used here rather than recomputed."""
    result = gross_pay(
        an_input(
            pay_basis=PayBasis.DAILY,
            days=(
                a_day(ordinary=SHIFT, days_equivalent=Decimal("1.000")),
                a_day(ordinary=Decimal("4.50"), days_equivalent=Decimal("0.500")),
            ),
        )
    )

    assert amount_of(result, "BASIC") == Decimal("1.500") * DAILY


def test_a_daily_employee_is_paid_a_full_day_for_a_public_holiday_not_worked():
    result = gross_pay(
        an_input(pay_basis=PayBasis.DAILY, days=(a_day(HOLIDAY, DayType.PUBLIC_HOLIDAY),))
    )

    assert amount_of(result, "BASIC") == DAILY


def test_an_instrument_that_does_not_pay_an_unworked_holiday_pays_nothing_for_it():
    """``public_holiday_not_worked_paid`` is a column, not an assumption. Every
    instrument this build loads sets it TRUE; the branch exists because the
    column does."""
    rates = dataclasses_replace(BCEA_RATES, public_holiday_not_worked_paid=False)

    result = gross_pay(an_input(rates=rates, days=(a_day(HOLIDAY, DayType.PUBLIC_HOLIDAY),)))

    assert amount_of(result, "BASIC") == Decimal("0")


def dataclasses_replace(rates: PremiumRates, **changes) -> PremiumRates:
    import dataclasses

    return dataclasses.replace(rates, **changes)


@pytest.mark.parametrize("basis", [PayBasis.WEEKLY, PayBasis.FORTNIGHTLY, PayBasis.MONTHLY])
def test_a_salaried_basic_is_the_period_rate_whatever_the_hours(basis):
    """A salaried employee who works a short day is paid their salary. Only an
    unpaid absence moves it."""
    result = gross_pay(
        salaried(
            pay_basis=basis,
            days=(a_day(ordinary=Decimal("4.00"), days_equivalent=Decimal("0.444")),),
        )
    )

    assert amount_of(result, "BASIC") == MONTHLY_SALARY


def test_an_unpaid_day_is_pro_rated_over_the_periods_own_working_days():
    result = gross_pay(
        salaried(
            days=(
                a_day(ordinary=SHIFT, days_equivalent=Decimal("1.000")),
                a_day(day_type=DayType.ABSENT_UNPAID),
            )
        )
    )

    expected = MONTHLY_SALARY * (WORKING_DAYS - 1) / WORKING_DAYS
    assert expected == Decimal("8357.142857142857142857142857")
    assert amount_of(result, "BASIC") == Decimal("8357.142857"), "six places, invariant 6"
    assert any("1 unpaid day(s) pro-rated" in w for w in result.trace.warnings)


def test_an_unpaid_day_with_no_working_days_to_divide_by_is_refused():
    with pytest.raises(GrossPayRefusedError, match="nothing to.*divide by"):
        gross_pay(
            salaried(
                working_days_in_period=Decimal("0"),
                days=(a_day(day_type=DayType.ABSENT_UNPAID),),
            )
        )


def test_more_unpaid_days_than_the_period_has_is_refused_rather_than_paid_negative():
    with pytest.raises(GrossPayRefusedError, match="negative salary"):
        gross_pay(
            salaried(
                working_days_in_period=Decimal("1"),
                days=(
                    a_day(day_type=DayType.ABSENT_UNPAID),
                    a_day(day_type=DayType.ABSENT_UNPAID),
                ),
            )
        )


def test_leave_is_not_priced_here_and_a_salaried_employee_loses_nothing_for_it():
    """D-166: LEAVE_PAY is its own component. An hourly employee's leave day
    carries no worked hours so BASIC excludes it; a salaried employee's salary
    is untouched."""
    hourly = gross_pay(an_input(days=(a_day(day_type=DayType.LEAVE),)))
    monthly = gross_pay(salaried(days=(a_day(day_type=DayType.LEAVE),)))

    assert amount_of(hourly, "BASIC") == Decimal("0")
    assert amount_of(monthly, "BASIC") == MONTHLY_SALARY


# ----------------------------------------------------------------- refusals


def test_a_standby_day_is_refused_by_name():
    day = a_day()
    standby = type(day)(
        day=AttendanceDayInput(
            work_date=MARCH,
            day_type=DayType.ORDINARY,
            time_in=None,
            time_out=None,
            unpaid_break_minutes=0,
            is_standby=True,
            is_ordinary_working_day=True,
            scheduled_ordinary_hours=SHIFT,
        ),
        hours=day.hours,
    )

    with pytest.raises(GrossPayRefusedError) as raised:
        gross_pay(an_input(days=(standby,)))

    message = str(raised.value)
    assert "O-19" in message and "O-20" in message and "O-22" in message


@pytest.mark.parametrize(
    ("bucket", "named"),
    [("overtime", "overtime"), ("sunday", "Sunday"), ("night", "night")],
)
def test_a_high_earner_is_refused_where_the_unread_exclusions_would_bite(bucket, named):
    """s6(3) has the MINISTER determine which provisions fall away above the
    threshold, and that determination has not been read into this build (O-24).
    Paying s10, s16 or s17(2) in full might be wrong; refusing cannot be."""
    day = a_day(ordinary=SHIFT, **{bucket: Decimal("2.00")})

    with pytest.raises(GrossPayRefusedError) as raised:
        gross_pay(an_input(days=(day,), above_bcea_earnings_threshold=True))

    assert named in str(raised.value)
    assert "O-24" in str(raised.value)


def test_a_high_earner_with_nothing_excluded_in_the_period_is_priced_normally():
    result = gross_pay(an_input(days=(a_day(ordinary=SHIFT),), above_bcea_earnings_threshold=True))

    assert amount_of(result, "BASIC") == SHIFT * HOURLY


def test_a_high_earner_loses_section_18_subsection_3_and_keeps_the_rest_of_section_18():
    """CLAUDE.md's own settled reading. On a public holiday the employee would
    not ordinarily work, s18(3)'s extra daily wage falls away and only the hours
    worked are owed; on one they WOULD work, s18(2) is untouched."""
    hours = Decimal("5.00")
    not_ordinary = a_day(
        HOLIDAY,
        DayType.PUBLIC_HOLIDAY,
        is_ordinary_working_day=False,
        scheduled=Decimal("0"),
        public_holiday=hours,
    )

    high = gross_pay(an_input(days=(not_ordinary,), above_bcea_earnings_threshold=True))
    ordinary_earner = gross_pay(an_input(days=(not_ordinary,)))

    assert amount_of(high, "PH_WORKED") == hours * HOURLY
    assert amount_of(ordinary_earner, "PH_WORKED") == DAILY + hours * HOURLY

    would_work = a_day(HOLIDAY, DayType.PUBLIC_HOLIDAY, public_holiday=hours)
    assert (
        amount_of(
            gross_pay(an_input(days=(would_work,), above_bcea_earnings_threshold=True)),
            "PH_WORKED",
        )
        == Decimal("2") * DAILY
    )


def test_a_premium_fully_covered_by_the_basic_produces_no_line_at_all():
    """A daily employee's short Sunday: s16(2) floors the entitlement at one
    daily wage, and the daily basic already paid exactly that. Nothing further
    is owed, and a zero line on a payslip is a question nobody can answer."""
    result = gross_pay(
        an_input(
            pay_basis=PayBasis.DAILY,
            days=(
                a_day(
                    SUNDAY,
                    DayType.SUNDAY,
                    is_ordinary_working_day=True,
                    sunday=Decimal("2.00"),
                    days_equivalent=Decimal("1.000"),
                ),
            ),
        )
    )

    assert amount_of(result, "SUNDAY_2_0") == Decimal("0")
    assert [line.component_code for line in result.lines] == ["BASIC"]


# ------------------------------------------------------------ night allowance


def test_a_fixed_amount_night_allowance_is_paid_per_shift_not_per_hour():
    rates = dataclasses_replace(
        BCEA_RATES,
        night_allowance_type=NightAllowanceKind.FIXED_AMOUNT,
        night_allowance_value=Decimal("25.0000"),
    )

    result = gross_pay(
        an_input(
            rates=rates,
            days=(
                a_day(ordinary=SHIFT, night=Decimal("4.00")),
                a_day(ordinary=SHIFT, night=Decimal("2.00")),
                a_day(ordinary=SHIFT),
            ),
        )
    )

    line = next(line for line in result.lines if line.component_code == "NIGHT_ALLOW")
    assert line.units == Decimal("2"), "two shifts with night hours, not six hours"
    assert line.amount.exact == Decimal("50.000000")


def test_time_off_in_place_of_an_allowance_pays_nothing_and_warns_about_nothing():
    """s17(2)(a)'s other limb. The obligation is discharged by reducing hours,
    so there is nothing owing and nothing to flag."""
    rates = dataclasses_replace(
        BCEA_RATES,
        night_allowance_type=NightAllowanceKind.TIME_OFF,
        night_allowance_value=None,
    )

    result = gross_pay(an_input(rates=rates, days=(a_day(ordinary=SHIFT, night=Decimal("3.00")),)))

    assert amount_of(result, "NIGHT_ALLOW") == Decimal("0")
    assert result.trace.warnings == ()


# -------------------------------------------------------------- the contract


def test_the_rates_refuse_a_float():
    with pytest.raises(TypeError, match="not Decimal"):
        PremiumRates(
            overtime_multiplier=1.5,
            sunday_multiplier_ordinary=Decimal("1.5"),
            sunday_multiplier_non_ordinary=Decimal("2"),
            public_holiday_worked_multiplier=Decimal("2"),
            public_holiday_not_worked_paid=True,
            night_allowance_type=NightAllowanceKind.BY_AGREEMENT,
            night_allowance_value=None,
            table="working_time_rule_set",
            row_id=1,
        )


def test_the_trace_records_the_rule_set_row_and_every_line():
    result = gross_pay(an_input(days=(a_day(ordinary=SHIFT, overtime=Decimal("1.00")),)))

    assert result.trace.calculator == "gross.gross_pay"
    assert result.trace.statutory_rows == (("working_time_rule_set", 31),)
    assert result.trace.outputs["line_BASIC"] == "405.000000"
    assert result.trace.outputs["line_OT_1_5"] == "67.500000"
    assert result.trace.outputs["gross"] == "472.500000"


def test_a_bucketing_warning_travels_through_to_the_pay_trace():
    day = a_day(ordinary=SHIFT)
    with_warning = type(day)(
        day=day.day,
        hours=type(day.hours)(
            ordinary_hours=SHIFT,
            overtime_hours=Decimal("0"),
            sunday_hours=Decimal("0"),
            public_holiday_hours=Decimal("0"),
            night_hours=Decimal("0"),
            paid_hours_guaranteed=Decimal("0"),
            standby_hours_worked=Decimal("0"),
            days_worked_equivalent=Decimal("1.000"),
            warnings=("night hours could not be determined from a bare total",),
        ),
    )

    result = gross_pay(an_input(days=(with_warning,)))

    assert "night hours could not be determined" in result.trace.warnings[0]


# ------------------------------------------- the two calculators fit together


NINE_HOUR_RULES = RuleFigures(
    ordinary_hours_per_week=Decimal("45"),
    ordinary_hours_per_day_5day=Decimal("9"),
    ordinary_hours_per_day_6day=Decimal("8"),
    overtime_multiplier=Decimal("1.500"),
    max_overtime_hours_per_day=Decimal("3"),
    max_overtime_hours_per_week=Decimal("10"),
    sunday_multiplier_ordinary=Decimal("1.500"),
    sunday_multiplier_non_ordinary=Decimal("2.000"),
    public_holiday_worked_multiplier=Decimal("2.000"),
    public_holiday_not_worked_paid=True,
    night_work_start_time=datetime.time(18, 0),
    night_work_end_time=datetime.time(6, 0),
    night_allowance_type="by_agreement",
    night_allowance_value=None,
    standby_allowance_per_shift=Decimal("0"),
    standby_window_start=datetime.time(0, 0),
    standby_window_end=datetime.time(0, 0),
    standby_hours_before_overtime=Decimal("0"),
    min_paid_hours_per_day=Decimal("4"),
    meal_interval_after_hours=Decimal("5"),
    meal_interval_minutes=60,
    daily_rest_hours=12,
    weekly_rest_hours=36,
)


def test_a_real_bucketed_day_prices_without_a_hand_built_result():
    """``bucket_day()`` produces the hours and ``gross_pay()`` prices them, with
    nothing in between. Eleven hours on an ordinary nine-hour day: nine ordinary
    and two overtime, so R405 basic and R135 overtime."""
    day = AttendanceDayInput(
        work_date=MARCH,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(7, 0),
        time_out=datetime.time(19, 0),
        unpaid_break_minutes=60,
        is_standby=False,
        is_ordinary_working_day=True,
        scheduled_ordinary_hours=SHIFT,
    )
    bucketed = bucket_day(day, NINE_HOUR_RULES)
    assert (bucketed.ordinary_hours, bucketed.overtime_hours) == (Decimal("9.00"), Decimal("2.00"))

    from calculators.gross import DayPay

    result = gross_pay(an_input(days=(DayPay(day=day, hours=bucketed),)))

    assert amount_of(result, "BASIC") == Decimal("405.000000")
    assert amount_of(result, "OT_1_5") == Decimal("135.000000")
    assert result.gross.rounded == Decimal("540.00")


# ---------------------------------------------------------------- properties


HOURS = st.decimals(min_value=Decimal("0"), max_value=Decimal("12"), places=2, allow_nan=False)


@settings(max_examples=200, deadline=None)
@given(ordinary=HOURS, overtime=HOURS, sunday=HOURS)
def test_gross_is_never_negative_and_never_less_than_the_basic(ordinary, overtime, sunday):
    days = (
        a_day(ordinary=ordinary, overtime=overtime),
        a_day(
            SUNDAY,
            DayType.SUNDAY,
            is_ordinary_working_day=False,
            scheduled=Decimal("0"),
            sunday=sunday,
        ),
    )

    result = gross_pay(an_input(days=days))

    assert result.gross.exact >= Decimal("0")
    assert result.gross.exact >= amount_of(result, "BASIC")


@settings(max_examples=200, deadline=None)
@given(sunday=st.decimals(min_value=Decimal("0.25"), max_value=Decimal("12"), places=2))
def test_a_sunday_never_pays_less_than_the_statutory_minimum_for_the_day(sunday):
    """s16(1) and s16(2) together: whatever else happens, the day is worth at
    least double the hours and at least a daily wage when the shift was short.
    Checked as a TOTAL, because the premium line is only the part the basic did
    not already cover."""
    day = a_day(
        SUNDAY,
        DayType.SUNDAY,
        is_ordinary_working_day=False,
        scheduled=Decimal("0"),
        sunday=sunday,
        days_equivalent=Decimal("0"),
    )

    result = gross_pay(an_input(days=(day,)))

    owed = sunday * HOURLY * Decimal("2")
    if sunday < SHIFT:
        owed = max(owed, DAILY)
    assert result.gross.exact >= owed
