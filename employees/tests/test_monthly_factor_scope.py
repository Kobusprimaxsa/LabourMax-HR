"""Two binding monthly factors, and which employee gets which (D-236).

BCEA s35(3) makes monthly remuneration four and ONE-THIRD times weekly. The
BCCCI Main Agreement's clause 3 makes it **4.33** for KwaZulu-Natal contract
cleaning. Both are in force, on different employees, and the difference is a
real rand figure on a real payslip.

D-104 settled that rate derivation has exactly one statutory constant, held in
``statutory_parameter`` and handed into ``employees/rates.py`` rather than known
by it. That is still true — ``rates.py`` still knows nothing. What changed is
that the constant is now RESOLVED on the employee's sector and workplace area
instead of being one global row.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from employees import rates
from employees.remuneration import MONTHLY_FACTOR_PARAMETER
from statutory import resolve
from statutory.models import Sector, SectorArea, StatutoryParameter

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

#: BCEA s35(3), unscoped — the fallback for everyone the agreement misses.
BCEA_FACTOR = Decimal("4.333333")
#: BCCCI clause 3, scoped to contract cleaning Area B.
BCCCI_FACTOR = Decimal("4.330000")

#: An hourly employee on the BCCCI's own 1 April 2026 rate, 45 hours a week.
HOURLY = Decimal("32.4000")
PATTERN = rates.WorkingPattern(
    hours_per_day=Decimal("9"), days_per_week=Decimal("5"), hours_per_week=Decimal("45")
)
WEEKLY = Decimal("1458.000000")

ON = datetime.date(2026, 6, 1)


@pytest.fixture
def instruments(db):
    cleaning = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    domestic = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    area_b = SectorArea.objects.create(
        sector=cleaning, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    area_a = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")

    StatutoryParameter.objects.create(
        parameter_code=MONTHLY_FACTOR_PARAMETER,
        value_numeric=BCEA_FACTOR,
        unit=StatutoryParameter.Unit.RATIO,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s35(3)",
    )
    StatutoryParameter.objects.create(
        parameter_code=MONTHLY_FACTOR_PARAMETER,
        sector=cleaning,
        sector_area=area_b,
        value_numeric=BCCCI_FACTOR,
        unit=StatutoryParameter.Unit.RATIO,
        effective_from=datetime.date(2026, 4, 1),
        source_reference="BCCCI Main Collective Agreement, GN R.7296, clause 3",
    )
    return {"cleaning": cleaning, "domestic": domestic, "area_b": area_b, "area_a": area_a}


def factor_for(instruments, sector, area=None) -> Decimal:
    return resolve.parameter_value(MONTHLY_FACTOR_PARAMETER, ON, sector=sector, sector_area=area)


# ------------------------------------------------- A2b: the actual rand figures


def test_a_kzn_cleaner_and_a_domestic_worker_derive_different_monthly_figures(instruments):
    """The rand figures, not merely that they differ — two wrong numbers also
    differ. R1 458,00 a week at 4,33 is R6 313,14; at four and one-third it is
    R6 317,999514. The employee is R4,86 a month apart depending on which
    instrument governs them."""
    kzn = factor_for(instruments, instruments["cleaning"], instruments["area_b"])
    household = factor_for(instruments, instruments["domestic"])

    assert kzn == BCCCI_FACTOR
    assert household == BCEA_FACTOR

    kzn_rates = rates.derive("hourly", HOURLY, PATTERN, kzn)
    household_rates = rates.derive("hourly", HOURLY, PATTERN, household)

    assert kzn_rates.weekly == WEEKLY == household_rates.weekly, "the week is the same"
    assert kzn_rates.monthly == Decimal("6313.140000")
    assert household_rates.monthly == Decimal("6317.999514")
    assert household_rates.monthly - kzn_rates.monthly == Decimal("4.859514")


def test_contract_cleaning_outside_kwazulu_natal_keeps_the_bcea_factor(instruments):
    """The agreement binds KwaZulu-Natal. An Area A cleaner is in the same
    sector and is not covered by it."""
    assert factor_for(instruments, instruments["cleaning"], instruments["area_a"]) == BCEA_FACTOR
    assert factor_for(instruments, instruments["cleaning"]) == BCEA_FACTOR


def test_an_unscoped_lookup_is_unaffected_by_the_scoped_row(instruments):
    """Every caller that existed before this change passes no scope and must
    land exactly where it always did."""
    assert resolve.parameter_value(MONTHLY_FACTOR_PARAMETER, ON) == BCEA_FACTOR


# ------------------------------------ A2c: 4,33 and 4,333 must not cross over


def test_the_bccci_factor_is_never_resolved_for_a_domestic_employee(instruments):
    """4,33 and 4,333 look almost identical and mean different things in
    different instruments. This is the test that fails if the scope is ever
    widened by accident — a domestic worker resolving to the contract cleaning
    agreement's factor would be short R4,86 a month with nothing on the payslip
    to show why."""
    for area in (None, instruments["area_b"], instruments["area_a"]):
        assert factor_for(instruments, instruments["domestic"], area) == BCEA_FACTOR, (
            "a domestic employee resolves to BCEA s35(3) regardless of what area is "
            "passed: the BCCCI row is scoped to CONTRACT_CLEANING and an area belonging "
            "to another sector must never reach it"
        )


def test_the_december_bonus_weeks_are_a_different_figure_and_never_the_factor(instruments):
    """SD1's ``termination_rule_set.annual_bonus_weeks`` is 4.333 — the QUANTITY
    of the December bonus, in weeks — and the BCCCI's clause 3 factor is 4.33.
    Two figures, two instruments, two tables, and they look almost the same.

    Nothing may resolve one for the other, so this pins that they live in
    different places entirely: the factor is a ``statutory_parameter`` and the
    bonus quantity is a column on a rule set. There is no lookup that could
    return the wrong one, and this test fails the moment somebody creates one.
    """
    assert (
        StatutoryParameter.objects.filter(parameter_code=MONTHLY_FACTOR_PARAMETER)
        .exclude(value_numeric__in=[BCEA_FACTOR, BCCCI_FACTOR])
        .count()
        == 0
    ), "the only two factors are the BCEA's and the BCCCI's"

    from statutory.models import TerminationRuleSet

    assert not hasattr(TerminationRuleSet, "monthly_to_weekly_factor"), (
        "the December bonus rule set must not grow a monthly factor column: the "
        "factor is statutory_parameter's, resolved on scope, and a second home for "
        "it is how 4.33 and 4.333 come to be used for each other"
    )
    assert "annual_bonus_weeks" in {f.name for f in TerminationRuleSet._meta.local_fields}


def test_the_scoped_row_carries_its_own_citation(instruments):
    """A second value for one concept is only safe if each says where it came
    from — otherwise the next reader cannot tell which is the mistake."""
    scoped = StatutoryParameter.objects.get(
        parameter_code=MONTHLY_FACTOR_PARAMETER, sector__isnull=False
    )
    unscoped = StatutoryParameter.objects.get(
        parameter_code=MONTHLY_FACTOR_PARAMETER, sector__isnull=True
    )

    assert "GN R.7296" in scoped.source_reference and "clause 3" in scoped.source_reference
    assert "s35(3)" in unscoped.source_reference
