"""Rate derivation — no database, because there is nothing here that needs one.

The case worth stating up front: **weekly is the hub**. Every captured basis converts
to a weekly rate first, and hourly and daily both come off that. The alternative —
deriving daily as hourly × hours_per_day — gives a different answer the moment
``hours_per_day × days_per_week`` is not ``hours_per_week``, and an employee would
then be paid one figure for a day of leave and a different one for a day of work.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from employees.rates import RateDerivationError, WorkingPattern, derive, weekly_rate_from

#: BCEA s35's four and one-third, as it is stored. Handed in, never known by the
#: module under test — that is the whole point of the argument.
FACTOR = Decimal("4.333333")

STANDARD = WorkingPattern(
    hours_per_day=Decimal(9), days_per_week=Decimal(5), hours_per_week=Decimal(45)
)


# ------------------------------------------------------------------ the bases


def test_an_hourly_rate_keeps_its_captured_figure_exactly():
    result = derive("hourly", Decimal("30.23"), STANDARD, FACTOR)
    assert result.hourly == Decimal("30.230000")
    assert result.weekly == Decimal("1360.350000")


def test_a_monthly_salary_keeps_its_captured_figure_exactly():
    """THE ONE THE EMPLOYEE CHECKS AGAINST THEIR CONTRACT.

    Round-tripping R4,500 through a weekly rate and back lands a few cents away, and
    the contract says 4,500. So the captured basis is preserved and the others are
    derived from it, never the reverse.
    """
    result = derive("monthly", Decimal("4500"), STANDARD, FACTOR)
    assert result.monthly == Decimal("4500.000000")


def test_a_daily_rate_keeps_its_captured_figure_exactly():
    assert derive("daily", Decimal("272.07"), STANDARD, FACTOR).daily == Decimal("272.070000")


def test_fortnightly_is_two_weeks():
    fortnight = derive("fortnightly", Decimal("2720.70"), STANDARD, FACTOR)
    week = derive("weekly", Decimal("1360.35"), STANDARD, FACTOR)
    assert fortnight.hourly == week.hourly
    assert fortnight.monthly == week.monthly


def test_every_basis_describing_the_same_pay_derives_the_same_rates():
    """R30.23 an hour on a 45-hour week is R1,360.35 a week, however it was typed."""
    hourly = derive("hourly", Decimal("30.23"), STANDARD, FACTOR)
    weekly = derive("weekly", Decimal("1360.35"), STANDARD, FACTOR)
    daily = derive("daily", Decimal("272.07"), STANDARD, FACTOR)

    assert hourly.hourly == weekly.hourly == daily.hourly
    assert hourly.daily == weekly.daily == daily.daily


# ------------------------------------------------------- the hub, and why it is one


def test_daily_comes_off_the_weekly_rate_not_off_the_hourly_one():
    """THE ASYMMETRY THAT IS NOT A BUG.

    A 40-hour week over 5 days is an 8-hour day, so hourly × 9 is wrong for this
    employee. Deriving daily from the weekly rate gives the figure they are actually
    owed for a day; deriving it from hourly × hours_per_day would pay them for nine
    hours they do not work.

    hours_per_day is deliberately left at 9 here while hours_per_week is 40, because
    that inconsistency is exactly what an employer types when they change one field
    and not the other.
    """
    pattern = WorkingPattern(
        hours_per_day=Decimal(9), days_per_week=Decimal(5), hours_per_week=Decimal(40)
    )
    result = derive("weekly", Decimal("1000"), pattern, FACTOR)

    assert result.daily == Decimal("200.000000"), "1000 over five days."
    assert result.hourly == Decimal("25.000000"), "1000 over forty hours."
    assert result.hourly * pattern.hours_per_day != result.daily


def test_a_consistent_pattern_reconciles_both_ways():
    """When hours_per_day × days_per_week does equal hours_per_week, they agree."""
    result = derive("weekly", Decimal("1350"), STANDARD, FACTOR)
    assert result.hourly * STANDARD.hours_per_day == result.daily


# ---------------------------------------------------------------- the factor


def test_the_statutory_factor_is_handed_in_and_actually_used():
    """Change the factor and the monthly rate changes. Nothing is baked in.

    If this ever passes with the two results equal, the module has grown its own
    copy of BCEA s35 and the reference table has stopped mattering.
    """
    real = derive("weekly", Decimal("1000"), STANDARD, FACTOR)
    hypothetical = derive("weekly", Decimal("1000"), STANDARD, Decimal("5"))

    assert real.monthly == Decimal("4333.333000")
    assert hypothetical.monthly == Decimal("5000.000000")


def test_a_factor_of_zero_is_refused_rather_than_dividing():
    with pytest.raises(RateDerivationError) as caught:
        derive("monthly", Decimal("4500"), STANDARD, Decimal(0))
    assert "MONTHLY_TO_WEEKLY_FACTOR" in str(caught.value)


# ------------------------------------------------------------------ refusals


def test_a_zero_or_negative_rate_is_refused():
    for amount in (Decimal(0), Decimal("-1")):
        with pytest.raises(RateDerivationError):
            derive("monthly", amount, STANDARD, FACTOR)


def test_an_unknown_basis_names_the_five_that_exist():
    with pytest.raises(RateDerivationError) as caught:
        derive("annually", Decimal("54000"), STANDARD, FACTOR)
    assert "fortnightly" in str(caught.value)


@pytest.mark.parametrize(
    "pattern",
    [
        WorkingPattern(Decimal(9), Decimal(5), Decimal(0)),
        WorkingPattern(Decimal(9), Decimal(0), Decimal(45)),
        WorkingPattern(Decimal(0), Decimal(5), Decimal(45)),
    ],
)
def test_a_zero_in_the_working_pattern_is_refused_rather_than_dividing(pattern):
    """A ZeroDivisionError three layers down says nothing about which field is blank."""
    with pytest.raises(RateDerivationError):
        weekly_rate_from("weekly", Decimal("1000"), pattern, FACTOR)


# ------------------------------------------------------------------ precision


def test_rates_are_stored_at_six_decimals_not_rounded_to_cents():
    """Invariant 6: intermediates carry four to six places, and rounding to two
    happens once, at the payslip line, long after this."""
    result = derive("monthly", Decimal("4500"), STANDARD, FACTOR)
    assert result.hourly == Decimal("23.076925")
    assert result.hourly.as_tuple().exponent == -6


def test_nothing_here_is_a_float():
    result = derive("monthly", Decimal("4500"), STANDARD, FACTOR)
    for value in (result.hourly, result.daily, result.monthly, result.weekly):
        assert isinstance(value, Decimal)
