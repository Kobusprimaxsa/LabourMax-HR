"""Gross pay against the Basic Conditions of Employment Act's own words.

There is no published DEL worked example for premium pay — D-150 settled that
for hour bucketing and the same is true here. What IS published is the sections
themselves, and each one states a rule precise enough to reproduce exactly. So
this file quotes the section and asserts the arithmetic it dictates, the same
shape as SDL's golden test (which reproduces the published RATE, there being no
worked figure to reproduce).

Every quotation below is from the Act as published in Government Gazette 18491
of 5 December 1997, Act No. 75 of 1997.

**Two of these were read rather than inferred, and both changed the code.**
s18 prices a DAY, not the hours in it, so a short public holiday shift is worth
two days' wages and not two hours'; and s16(2) puts a daily-wage floor under a
short Sunday. A multiplier column named ``public_holiday_worked_multiplier``
invites exactly the wrong reading, which is why the section is transcribed here
beside the test that holds the code to it.

**Marked ``statute``, not ``golden``** (D-283). These reproduce what a section's
own words dictate, not a figure a regulator published, so they are not part of
the golden set behind ``--golden-tests-passed`` — they run with every other test.
Until 24 September 2026 they carried the ``golden`` mark and the file said
"_golden", which made the flag's meaning depend on which files one counted.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.attendance import AttendanceDayInput, AttendanceDayResult, DayType
from calculators.gross import (
    DayPay,
    GrossInput,
    NightAllowanceKind,
    PayBasis,
    PremiumRates,
    gross_pay,
)

pytestmark = pytest.mark.statute

MARCH = datetime.date(2026, 3, 31)
SUNDAY = datetime.date(2026, 3, 1)
HOLIDAY = datetime.date(2026, 3, 21)  # Human Rights Day

#: R45.00 an hour on a nine-hour day — R405.00 a day, the two consistent with
#: each other the way employees/rates.py derives them (D-106).
HOURLY = Decimal("45.00")
DAILY = Decimal("405.00")
SHIFT = Decimal("9.00")

#: The BCEA default rule set as ref-2026.03.01-rules.json loads it.
BCEA_RATES = PremiumRates(
    overtime_multiplier=Decimal("1.500"),
    sunday_multiplier_ordinary=Decimal("1.500"),
    sunday_multiplier_non_ordinary=Decimal("2.000"),
    public_holiday_worked_multiplier=Decimal("2.000"),
    public_holiday_not_worked_paid=True,
    night_allowance_type=NightAllowanceKind.BY_AGREEMENT,
    night_allowance_value=None,
    table="working_time_rule_set",
    row_id=31,
)

#: SD1, the one instrument in either sector that states a night figure: 10% of
#: the hourly wage between 18:00 and 06:00.
SD1_RATES = PremiumRates(
    overtime_multiplier=Decimal("1.500"),
    sunday_multiplier_ordinary=Decimal("1.500"),
    sunday_multiplier_non_ordinary=Decimal("2.000"),
    public_holiday_worked_multiplier=Decimal("2.000"),
    public_holiday_not_worked_paid=True,
    night_allowance_type=NightAllowanceKind.PERCENTAGE,
    night_allowance_value=Decimal("10.0000"),
    table="working_time_rule_set",
    row_id=33,
)


def a_day(
    work_date=MARCH,
    day_type=DayType.ORDINARY,
    *,
    is_ordinary_working_day=True,
    scheduled=SHIFT,
    ordinary=Decimal("0"),
    overtime=Decimal("0"),
    sunday=Decimal("0"),
    public_holiday=Decimal("0"),
    night=Decimal("0"),
    guarantee=Decimal("0"),
    days_equivalent=Decimal("0"),
) -> DayPay:
    """One day, as captured and as bucketed. The buckets are stated rather than
    produced by ``bucket_day()`` so a test can put exactly one thing in play;
    ``test_gross.py`` runs the two calculators end to end against each other."""
    return DayPay(
        day=AttendanceDayInput(
            work_date=work_date,
            day_type=day_type,
            time_in=None,
            time_out=None,
            unpaid_break_minutes=0,
            is_standby=False,
            is_ordinary_working_day=is_ordinary_working_day,
            scheduled_ordinary_hours=scheduled,
        ),
        hours=AttendanceDayResult(
            ordinary_hours=ordinary,
            overtime_hours=overtime,
            sunday_hours=sunday,
            public_holiday_hours=public_holiday,
            night_hours=night,
            paid_hours_guaranteed=guarantee,
            standby_hours_worked=Decimal("0"),
            days_worked_equivalent=days_equivalent,
        ),
    )


def an_input(**overrides) -> GrossInput:
    values = {
        "calculated_for": MARCH,
        "pay_basis": PayBasis.HOURLY,
        "days": (),
        "rates": BCEA_RATES,
        "hourly_rate": HOURLY,
        "daily_rate": DAILY,
        "ordinary_shift_hours": SHIFT,
    }
    values.update(overrides)
    return GrossInput(**values)


def amount_of(result, code) -> Decimal:
    for line in result.lines:
        if line.component_code == code:
            return line.amount.exact
    return Decimal("0")


# ------------------------------------------------------------ s10: overtime


def test_overtime_is_one_and_one_half_times_the_wage_for_each_hour():
    """s10(2): "An employer must pay an employee at least one and one-half
    times the employee's wage for overtime worked."

    Per hour, and outside the basic on every basis, so the full multiplier is
    what the line carries.
    """
    result = gross_pay(an_input(days=(a_day(ordinary=SHIFT, overtime=Decimal("2.00")),)))

    assert amount_of(result, "BASIC") == SHIFT * HOURLY
    assert amount_of(result, "OT_1_5") == Decimal("2.00") * HOURLY * Decimal("1.5")
    assert amount_of(result, "OT_1_5") == Decimal("135.000000")


# ------------------------------------------------------------- s16: Sundays


def test_a_sunday_not_ordinarily_worked_is_double_the_wage_for_each_hour():
    """s16(1): "An employer must pay an employee who works on a Sunday at
    double the employee's wage for each hour worked, unless the employee
    ordinarily works on a Sunday ...".
    """
    result = gross_pay(
        an_input(
            days=(
                a_day(
                    SUNDAY,
                    DayType.SUNDAY,
                    is_ordinary_working_day=False,
                    scheduled=Decimal("0"),
                    sunday=SHIFT,
                ),
            )
        )
    )

    assert amount_of(result, "SUNDAY_2_0") == SHIFT * HOURLY * Decimal("2")
    assert amount_of(result, "SUNDAY_2_0") == Decimal("810.000000")


def test_a_sunday_that_is_an_ordinary_working_day_is_one_and_one_half_times():
    """s16(1), the second limb: "... in which case the employer must pay the
    employee at one and one-half times the employee's wage for each hour
    worked." The distinction is per employee, not per employer."""
    result = gross_pay(
        an_input(days=(a_day(SUNDAY, DayType.SUNDAY, is_ordinary_working_day=True, sunday=SHIFT),))
    )

    assert amount_of(result, "SUNDAY_2_0") == SHIFT * HOURLY * Decimal("1.5")
    assert amount_of(result, "SUNDAY_2_0") == Decimal("607.500000")


def test_a_short_sunday_pays_a_full_daily_wage():
    """s16(2): "If an employee works less than the employee's ordinary shift on
    a Sunday and the payment that the employee is entitled to in terms of
    subsection (1) is less than the employee's ordinary daily wage, the employer
    must pay the employee the employee's ordinary daily wage."

    Two hours at double time is R180; the ordinary daily wage is R405. The floor
    is what is owed, and reading s16(1) alone underpays by R225.
    """
    two_hours = Decimal("2.00")
    result = gross_pay(
        an_input(
            days=(
                a_day(
                    SUNDAY,
                    DayType.SUNDAY,
                    is_ordinary_working_day=False,
                    scheduled=Decimal("0"),
                    sunday=two_hours,
                ),
            )
        )
    )

    assert two_hours * HOURLY * Decimal("2") == Decimal("180.00"), "what s16(1) alone gives"
    assert amount_of(result, "SUNDAY_2_0") == DAILY


def test_a_full_sunday_shift_is_not_floored():
    """The floor only applies where the employee "works less than the
    employee's ordinary shift". A full shift at double time is well above the
    daily wage anyway, but the condition is tested, not the consequence."""
    result = gross_pay(
        an_input(
            days=(
                a_day(
                    SUNDAY,
                    DayType.SUNDAY,
                    is_ordinary_working_day=False,
                    scheduled=Decimal("0"),
                    sunday=SHIFT,
                ),
            )
        )
    )

    assert amount_of(result, "SUNDAY_2_0") > DAILY


# ------------------------------------------------------ s18: public holidays


def test_a_public_holiday_worked_pays_double_the_day_not_double_the_hours():
    """s18(2): "If a public holiday falls on a day on which an employee would
    ordinarily work, an employer must pay — ... (b) an employee who does work on
    the public holiday — (i) at least double the amount referred to in paragraph
    (a); or (ii) if it is greater, the amount referred to in paragraph (a) plus
    the amount earned by the employee for the time worked on that day."

    Paragraph (a) is "at least the wage that the employee would ordinarily have
    received for work on that day" — a DAY's wage. So four hours on a public
    holiday is two days' pay (R810), not four hours at double time (R360).
    Pricing this as hours × wage × 2 underpays every short holiday shift, and
    the column is called ``public_holiday_worked_multiplier``, which is exactly
    the invitation to get it wrong.
    """
    four = Decimal("4.00")
    result = gross_pay(
        an_input(days=(a_day(HOLIDAY, DayType.PUBLIC_HOLIDAY, public_holiday=four),))
    )

    assert four * HOURLY * Decimal("2") == Decimal("360.00"), "the wrong reading"
    assert amount_of(result, "PH_WORKED") == Decimal("2") * DAILY
    assert amount_of(result, "PH_WORKED") == Decimal("810.000000")


def test_a_long_public_holiday_shift_takes_the_greater_of_the_two_limbs():
    """s18(2)(b)(ii): "if it is greater, the amount referred to in paragraph (a)
    plus the amount earned by the employee for the time worked on that day."

    Twelve hours: double the day is R810, the day plus the hours earned is
    405 + 540 = R945. The Act says the greater.
    """
    twelve = Decimal("12.00")
    result = gross_pay(
        an_input(days=(a_day(HOLIDAY, DayType.PUBLIC_HOLIDAY, public_holiday=twelve),))
    )

    assert amount_of(result, "PH_WORKED") == DAILY + twelve * HOURLY
    assert amount_of(result, "PH_WORKED") == Decimal("945.000000")


def test_a_public_holiday_on_a_day_the_employee_would_not_work_is_a_day_plus_the_hours():
    """s18(3): "If an employee works on a public holiday on which the employee
    would not ordinarily work, the employer must pay that employee an amount
    equal to — (a) the employee's ordinary daily wage; plus (b) the amount
    earned by the employee for the work performed that day."

    A day's wage PLUS the hours, and no doubling anywhere in it.
    """
    five = Decimal("5.00")
    result = gross_pay(
        an_input(
            days=(
                a_day(
                    HOLIDAY,
                    DayType.PUBLIC_HOLIDAY,
                    is_ordinary_working_day=False,
                    scheduled=Decimal("0"),
                    public_holiday=five,
                ),
            )
        )
    )

    assert amount_of(result, "PH_WORKED") == DAILY + five * HOURLY
    assert amount_of(result, "PH_WORKED") == Decimal("630.000000")


def test_a_public_holiday_not_worked_is_still_a_day_of_pay_and_it_is_basic():
    """s18(2)(a): "an employee who does not work on the public holiday, at least
    the wage that the employee would ordinarily have received for work on that
    day."

    ``days_worked_equivalent`` is 0.000 for a day with no hours in it, so an
    hourly employee would otherwise be paid nothing at all for it. It is BASIC
    rather than a premium — the component catalogue's own reasoning under
    PH_WORKED, which exists only for the holiday that WAS worked.
    """
    result = gross_pay(an_input(days=(a_day(HOLIDAY, DayType.PUBLIC_HOLIDAY),)))

    assert amount_of(result, "BASIC") == SHIFT * HOURLY
    assert amount_of(result, "PH_WORKED") == Decimal("0")


# ----------------------------------------------------------- s17: night work


def test_the_sd1_night_allowance_is_ten_per_cent_of_the_hourly_wage():
    """SD1's gazetted figure, and the only real one in either sector: 10% of the
    hourly wage for hours between 18:00 and 06:00. BCEA s17(2)(a) requires an
    allowance and states no amount, which is why every other instrument here
    carries NULL rather than a zero (O-22).
    """
    night = Decimal("4.00")
    result = gross_pay(an_input(rates=SD1_RATES, days=(a_day(ordinary=SHIFT, night=night),)))

    assert amount_of(result, "NIGHT_ALLOW") == night * HOURLY * Decimal("0.10")
    assert amount_of(result, "NIGHT_ALLOW") == Decimal("18.000000")


def test_an_instrument_stating_no_night_allowance_pays_nothing_and_says_so():
    """The BCEA requires an allowance "which may be a shift allowance, or by a
    reduction of working hours" and sets no figure. Paying nothing silently is
    how an employee is quietly short-changed, so the trace carries it."""
    result = gross_pay(an_input(days=(a_day(ordinary=SHIFT, night=Decimal("3.00")),)))

    assert amount_of(result, "NIGHT_ALLOW") == Decimal("0")
    assert any("states no allowance" in w for w in result.trace.warnings)


# ------------------------------------------- the three bases must agree
#
# The premium rules above state TOTALS for the day. Whether the basic already
# paid part of that total is a fact about the pay basis, so the same Sunday
# must cost the employer the same on all three — an employee moved from hourly
# to monthly for identical work cannot be paid a different amount.


@pytest.mark.parametrize(
    ("basis", "extra"),
    [
        (PayBasis.HOURLY, {}),
        (PayBasis.DAILY, {}),
        (
            PayBasis.MONTHLY,
            {"period_rate": Decimal("8775.00"), "working_days_in_period": Decimal("21")},
        ),
    ],
)
def test_a_sunday_not_ordinarily_worked_costs_the_same_on_every_basis(basis, extra):
    """Nothing in the basic pays for a Sunday the employee does not ordinarily
    work, on any basis — the hourly basic is the ordinary bucket, the daily
    equivalent is zero for a day with no ordinary hours in it, and a salary buys
    the ordinary working days. So all three pay the full s16(1) double time."""
    day = a_day(
        SUNDAY,
        DayType.SUNDAY,
        is_ordinary_working_day=False,
        scheduled=Decimal("0"),
        sunday=SHIFT,
        days_equivalent=Decimal("0"),
    )

    result = gross_pay(an_input(pay_basis=basis, days=(day,), **extra))

    assert amount_of(result, "SUNDAY_2_0") == SHIFT * HOURLY * Decimal("2")


@pytest.mark.parametrize(
    ("basis", "extra", "days_equivalent"),
    [
        (PayBasis.HOURLY, {}, Decimal("0")),
        (PayBasis.DAILY, {}, Decimal("1.000")),
        (
            PayBasis.MONTHLY,
            {"period_rate": Decimal("8775.00"), "working_days_in_period": Decimal("21")},
            Decimal("1.000"),
        ),
    ],
)
def test_a_sunday_that_is_ordinarily_worked_costs_the_same_on_every_basis(
    basis, extra, days_equivalent
):
    """Here the bases differ in what the basic already paid — nothing on hourly,
    a full day on daily, one ordinary daily wage out of the salary on monthly —
    so the premium differs by exactly that, and the TOTAL for the day does not.

    s16(1) second limb: one and a half times for each hour = R607.50 for nine
    hours. On hourly that is the whole premium; on the other two the basic
    already paid R405, so the premium is R202.50 and the day still costs R607.50.
    """
    day = a_day(
        SUNDAY,
        DayType.SUNDAY,
        is_ordinary_working_day=True,
        sunday=SHIFT,
        days_equivalent=days_equivalent,
    )

    result = gross_pay(an_input(pay_basis=basis, days=(day,), **extra))

    already_paid = Decimal("0") if basis is PayBasis.HOURLY else DAILY
    assert amount_of(result, "SUNDAY_2_0") == SHIFT * HOURLY * Decimal("1.5") - already_paid
    assert amount_of(result, "SUNDAY_2_0") + already_paid == Decimal("607.500000")
