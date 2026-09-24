"""The statutory annual bonus — SD1 clause 3(3) and BCCCI clause 4.5.

Neither instrument publishes a worked example, so these are ``statute`` tests
(D-283): the clause's own arithmetic, the figures computed by hand in the
docstrings, and nothing claiming to be golden.

SD1 clause 3(3)(b)(i), the whole formula: "The number of full calendar months
service divided by 12 and multiplied by four point three three three times the
employee's weekly wage."
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.bonus import (
    BonusInput,
    BonusInputError,
    BonusRule,
    RateBasis,
    WeeklyWage,
    annual_bonus,
    cycle_containing,
    full_months_of_service,
)
from calculators.termination import TerminationInput, termination_payout
from calculators.tests.test_termination_statute import FOUR_MONTHS

pytestmark = pytest.mark.statute

#: SD1 as ``ref-2026.03.01-sd1.json`` loads it: 4,333 weeks, December, pro rata
#: on termination, no minimum service.
SD1 = BonusRule(
    weeks=Decimal("4.333"),
    payment_month=12,
    pro_rata_on_termination=True,
    min_service_months=0,
    table="termination_rule_set",
    row_id=91,
)
#: The BCCCI's 4,33 — a different figure that looks almost the same.
BCCCI = BonusRule(
    weeks=Decimal("4.330"),
    payment_month=12,
    pro_rata_on_termination=True,
    min_service_months=0,
    table="termination_rule_set",
    row_id=92,
)
#: The BCEA default and SD7: no payment month, no weeks. No bonus at all.
NO_BONUS = BonusRule(
    weeks=Decimal("0.000"),
    payment_month=None,
    pro_rata_on_termination=False,
    min_service_months=0,
    table="termination_rule_set",
    row_id=93,
)

#: SD1 Area A's gazetted weekly rate from 1 March 2026 — R33,27 an hour on a
#: 45-hour week, GN R.7083 clause 3(1): "R1497,15".
WEEKLY = Decimal("1497.15")
CYCLE_2026 = (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))


def a_bonus(**overrides) -> BonusInput:
    values = {
        "calculated_for": datetime.date(2026, 12, 31),
        "rule": SD1,
        "cycle_start": CYCLE_2026[0],
        "cycle_end": CYCLE_2026[1],
        "service_start": datetime.date(2024, 2, 1),
        "service_end": None,
        "as_at": CYCLE_2026[1],
        "wages": (WeeklyWage(datetime.date(2024, 2, 1), None, WEEKLY),),
    }
    values.update(overrides)
    return BonusInput(**values)


# ------------------------------------------------------------------ the cycle


def test_a_december_bonus_cycle_is_the_calendar_year():
    assert cycle_containing(datetime.date(2026, 6, 20), 12) == CYCLE_2026
    assert cycle_containing(datetime.date(2026, 12, 31), 12) == CYCLE_2026
    assert cycle_containing(datetime.date(2027, 1, 1), 12)[0] == datetime.date(2027, 1, 1)


def test_the_cycle_follows_the_rows_payment_month_rather_than_assuming_december():
    """No December is written into the module: a June payment month makes a
    July-to-June cycle."""
    assert cycle_containing(datetime.date(2026, 3, 15), 6) == (
        datetime.date(2025, 7, 1),
        datetime.date(2026, 6, 30),
    )
    assert cycle_containing(datetime.date(2026, 7, 1), 6)[0] == datetime.date(2026, 7, 1)


# ------------------------------------------------------- (a): a full year


def test_a_full_year_is_four_point_three_three_three_weekly_wages():
    """3(3)(a). 4,333 × R1 497,15 = R6 487,15095 — which is also SD1's own
    gazetted monthly rate, R6487,15, since the monthly figure "is based on
    4.333 weeks"."""
    result = annual_bonus(a_bonus())

    assert result.full_months == 12
    assert result.amount.exact == Decimal("6487.150950")
    assert result.amount.rounded == Decimal("6487.15")


def test_the_bccci_bonus_is_four_point_three_three_not_four_point_three_three_three():
    result = annual_bonus(a_bonus(rule=BCCCI))
    assert result.amount.exact == Decimal("6482.659500"), "4,33 × R1 497,15"


# ------------------------------------------------- (b): full calendar months


def test_a_month_joined_after_the_first_earns_nothing():
    """Joined 15 February 2026: February is not a full calendar month of
    service, so March to December — ten months."""
    result = annual_bonus(a_bonus(service_start=datetime.date(2026, 2, 15)))
    assert result.full_months == 10
    assert result.months[0] == datetime.date(2026, 3, 1)


def test_the_bccci_election_counts_the_joining_month_in_full():
    """BONUS_PART_MONTH_EARNS_NOTHING = FALSE, BCCCI 4.5(c)(ii) under 4.5(g)."""
    result = annual_bonus(
        a_bonus(service_start=datetime.date(2026, 2, 15), part_first_month_counts=True)
    )
    assert result.full_months == 11


def test_joined_on_the_first_counts_that_month():
    assert annual_bonus(a_bonus(service_start=datetime.date(2026, 2, 1))).full_months == 11


def test_a_month_left_before_its_last_day_earns_nothing_and_its_last_day_earns_it():
    left_on_the_20th = a_bonus(
        service_end=datetime.date(2026, 6, 20), as_at=datetime.date(2026, 6, 20)
    )
    left_on_the_30th = a_bonus(
        service_end=datetime.date(2026, 6, 30), as_at=datetime.date(2026, 6, 30)
    )

    assert annual_bonus(left_on_the_20th).full_months == 5, "January to May"
    assert annual_bonus(left_on_the_30th).full_months == 6, "June is complete"


def test_an_accrual_counts_only_months_that_have_ended():
    """As at 31 May the cycle has earned five twelfths; as at 30 May, four."""
    assert annual_bonus(a_bonus(as_at=datetime.date(2026, 5, 31))).full_months == 5
    assert annual_bonus(a_bonus(as_at=datetime.date(2026, 5, 30))).full_months == 4


# ------------------------------------------------------------ which wage


def test_sd1_prices_every_month_at_the_wage_at_the_end():
    """ "the employee's weekly wage" — one wage, the one in force at payment."""
    raised = (
        WeeklyWage(datetime.date(2024, 2, 1), datetime.date(2026, 7, 1), Decimal("1400.00")),
        WeeklyWage(datetime.date(2026, 7, 1), None, WEEKLY),
    )
    result = annual_bonus(a_bonus(wages=raised))
    assert result.amount.exact == Decimal("6487.150950")


def test_the_bccci_default_prices_each_month_at_its_own_prevailing_rate():
    """BCCCI 4.5(d): six months at R1 400 and six at R1 497,15, each earning a
    twelfth of 4,33 weeks — (6 × 1 400 + 6 × 1 497,15) × 4,33 ÷ 12 =
    R6 272,32975, against R6 482,6595 had it all been at the December rate."""
    raised = (
        WeeklyWage(datetime.date(2024, 2, 1), datetime.date(2026, 7, 1), Decimal("1400.00")),
        WeeklyWage(datetime.date(2026, 7, 1), None, WEEKLY),
    )
    result = annual_bonus(a_bonus(rule=BCCCI, wages=raised, rate_basis=RateBasis.EACH_MONTH))
    assert result.amount.exact == Decimal("6272.329750")


def test_a_gap_in_the_wage_history_refuses():
    gap = (WeeklyWage(datetime.date(2026, 7, 1), None, WEEKLY),)
    with pytest.raises(BonusInputError, match="No weekly wage is in force on 31 January 2026"):
        annual_bonus(a_bonus(wages=gap, rate_basis=RateBasis.EACH_MONTH))


# ------------------------------------------------------------ no bonus at all


def test_an_instrument_with_no_bonus_refuses_rather_than_computing_nil():
    with pytest.raises(BonusInputError, match="gives no annual bonus"):
        annual_bonus(a_bonus(rule=NO_BONUS))


def test_an_instrument_that_does_not_pro_rate_pays_a_leaver_nothing_and_says_so():
    rule = BonusRule(Decimal("4.333"), 12, False, 0, "termination_rule_set", 94)
    result = annual_bonus(
        a_bonus(
            rule=rule,
            service_end=datetime.date(2026, 6, 20),
            as_at=datetime.date(2026, 6, 20),
            is_termination=True,
        )
    )
    assert result.amount.exact == Decimal("0")
    assert "no pro-rata bonus on termination" in result.trace.warnings[0]


def test_a_casual_excluded_by_the_bccci_default_earns_nothing_and_it_says_why():
    result = annual_bonus(
        a_bonus(rule=BCCCI, qualifies=False, disqualified_because="casual, BCCCI clause 4.5(f)")
    )
    assert result.amount.exact == Decimal("0")
    assert result.trace.warnings == ("No bonus: casual, BCCCI clause 4.5(f).",)


def test_a_minimum_service_requirement_is_read_off_the_row():
    rule = BonusRule(Decimal("4.333"), 12, True, 3, "termination_rule_set", 95)
    short = annual_bonus(
        a_bonus(rule=rule, service_start=datetime.date(2026, 10, 1)),
    )
    assert short.amount.exact == Decimal("0")
    assert "requires 3" in short.trace.warnings[0]
    assert full_months_of_service(datetime.date(2026, 10, 1), datetime.date(2026, 12, 31)) == 2


def test_a_cycle_that_ends_before_it_starts_refuses():
    with pytest.raises(BonusInputError, match="ends on"):
        annual_bonus(a_bonus(cycle_end=datetime.date(2025, 12, 31)))


# --------------------------------------------- the mid-year leaver, by hand


def test_a_mid_year_leavers_payout_carries_the_hand_computed_pro_rata_bonus():
    """The brief's proof, closing D-224's gap. An SD1 Area A cleaner on the
    gazetted R1 497,15 a week, employed since 1 February 2024, leaves on
    20 June 2026. The 2026 cycle is January to December; January to May are
    full calendar months and June is not. By hand:

        5 ÷ 12 × 4,333 × 1 497,15
        = 5 ÷ 12 × 6 487,15095
        = 32 435,75475 ÷ 12
        = 2 702,97956…  →  R2 702,98
    """
    leaving = datetime.date(2026, 6, 20)
    result = termination_payout(
        TerminationInput(
            calculated_for=leaving,
            weekly_rate=WEEKLY,
            daily_rate=Decimal("299.43"),
            hourly_rate=Decimal("33.27"),
            days_per_week=Decimal("5"),
            hours_per_week=Decimal("45"),
            pro_rata_minimum_service_months=FOUR_MONTHS,
            annual_bonus=a_bonus(
                calculated_for=leaving, service_end=leaving, as_at=leaving, is_termination=True
            ),
        )
    )

    (line,) = [line for line in result.lines if line.component_code == "BONUS_PRO_RATA"]
    assert line.units == Decimal("5")
    assert result.pro_rata_bonus.exact == Decimal("2702.979563")
    assert result.pro_rata_bonus.rounded == Decimal("2702.98")
    assert result.total.rounded == Decimal("2702.98")
    assert result.trace.outputs["pro_rata_bonus"] == "2702.979563"
    assert result.trace.inputs["bonus_as_at"] == "2026-06-20"
    assert ("termination_rule_set", 91) in result.trace.statutory_rows


def test_a_leaver_with_no_bonus_input_gets_no_bonus_line():
    result = termination_payout(
        TerminationInput(
            calculated_for=datetime.date(2026, 6, 20),
            weekly_rate=WEEKLY,
            daily_rate=Decimal("299.43"),
            hourly_rate=Decimal("33.27"),
            days_per_week=Decimal("5"),
            hours_per_week=Decimal("45"),
            pro_rata_minimum_service_months=FOUR_MONTHS,
        )
    )
    assert not [line for line in result.lines if line.component_code == "BONUS_PRO_RATA"]
    assert result.pro_rata_bonus.exact == Decimal("0")
    assert "pro_rata_bonus" in result.trace.outputs


def test_a_cycle_longer_than_twelve_months_refuses():
    """A caller that hands in two years as one cycle would pay two bonuses as
    one; ``cycle_containing()`` never builds such a cycle, and the calculator
    refuses one built by hand."""
    with pytest.raises(BonusInputError, match="24 months counted in one cycle"):
        annual_bonus(a_bonus(cycle_start=datetime.date(2025, 1, 1)))
