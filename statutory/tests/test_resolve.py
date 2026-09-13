"""``statutory.resolve`` — the only place that asks "what applied on this date".

The bug this module exists to prevent cannot be caught by reading code, because the
wrong version reads correctly: ``effective_to__gte`` instead of ``effective_to__gt``
looks right, passes casual review, and produces a payslip that is wrong only for
employees paid on the day a rate changed. So the boundary is asserted here, on every
lookup, at the exact day the row ends.

The other half of the file is the fallback order. Narrowing falls back — a sector
with no row of its own is priced at the National Minimum Wage, which genuinely is the
floor underneath it — and the order in which dimensions are dropped is fixed rather
than left to whatever the ORM returned first.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from statutory import resolve
from statutory.models import (
    JobGrade,
    LeaveRuleSet,
    MinimumWageRate,
    PayeRebate,
    PublicHoliday,
    Sector,
    SectorArea,
    StatutoryParameter,
    TaxYear,
)
from statutory.tests.test_rule_sets import rule_set

MARCH_2026 = datetime.date(2026, 3, 1)
MARCH_2027 = datetime.date(2027, 3, 1)
MID_2026 = datetime.date(2026, 9, 1)

CITATION = "Test fixture, not a real gazette"


@pytest.fixture
def domestic(db):
    return Sector.objects.create(
        code=Sector.Code.DOMESTIC,
        name="Domestic worker sector",
        determination_reference="Sectoral Determination 7",
    )


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING,
        name="Contract cleaning sector",
        determination_reference="Sectoral Determination 1",
        uses_area_rates=True,
    )


def wage(*, sector=None, area=None, grade=None, hourly, frm=MARCH_2026, to=None, band=None):
    return MinimumWageRate.objects.create(
        sector=sector,
        sector_area=area,
        job_grade=grade,
        hours_band=band or MinimumWageRate.HoursBand.ALL,
        hourly_rate=Decimal(hourly),
        effective_from=frm,
        effective_to=to,
        source_reference=CITATION,
    )


def parameter(*, code="TEST_CODE", value, frm=MARCH_2026, to=None, text=""):
    return StatutoryParameter.objects.create(
        parameter_code=code,
        value_numeric=None if value is None else Decimal(value),
        value_text=text,
        effective_from=frm,
        effective_to=to,
        source_reference=CITATION,
    )


# ------------------------------------------------------------ the date boundary


@pytest.mark.statutory
def test_a_period_covers_its_first_day(db):
    parameter(value="1.000000", frm=MARCH_2026, to=MARCH_2027)
    assert resolve.parameter_value("TEST_CODE", MARCH_2026) == Decimal("1.000000")


@pytest.mark.statutory
def test_a_period_does_not_cover_the_day_it_ends(db):
    """effective_to is exclusive. This is the assertion the whole module is for."""
    parameter(value="1.000000", frm=MARCH_2026, to=MARCH_2027)
    parameter(value="2.000000", frm=MARCH_2027, to=None)

    assert resolve.parameter_value("TEST_CODE", MARCH_2027 - datetime.timedelta(days=1)) == Decimal(
        "1.000000"
    )
    assert resolve.parameter_value("TEST_CODE", MARCH_2027) == Decimal("2.000000")


@pytest.mark.statutory
def test_a_date_before_the_first_row_raises_rather_than_guessing(db):
    parameter(value="1.000000", frm=MARCH_2026)
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.parameter_value("TEST_CODE", datetime.date(2025, 12, 31))


@pytest.mark.statutory
def test_an_open_ended_row_applies_indefinitely(db):
    parameter(value="1.000000", frm=MARCH_2026, to=None)
    assert resolve.parameter_value("TEST_CODE", datetime.date(2031, 1, 1)) == Decimal("1.000000")


@pytest.mark.statutory
def test_a_missing_parameter_raises_and_names_itself(db):
    with pytest.raises(resolve.StatutoryValueMissingError) as caught:
        resolve.parameter_value("UIF_MONTHLY_CEILING", MARCH_2026)
    assert "UIF_MONTHLY_CEILING" in str(caught.value)


@pytest.mark.statutory
def test_asking_a_text_parameter_for_a_number_raises(db):
    """Rather than failing later, inside arithmetic, where the message names nothing."""
    parameter(code="TEXT_CODE", value=None, text="employer declaration")
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.parameter_value("TEXT_CODE", MARCH_2026)


@pytest.mark.statutory
def test_parameter_or_none_is_the_variant_that_may_return_nothing(db):
    assert resolve.parameter_or_none("NOT_LOADED", MARCH_2026) is None


# ------------------------------------------------------- minimum wage fallbacks


@pytest.mark.statutory
def test_the_most_specific_wage_row_wins(db, cleaning):
    area = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")
    grade = JobGrade.objects.create(sector=cleaning, code="SUPERVISOR", name="Supervisor")

    wage(hourly="10.0000")
    wage(sector=cleaning, hourly="20.0000")
    wage(sector=cleaning, area=area, hourly="30.0000")
    wage(sector=cleaning, area=area, grade=grade, hourly="40.0000")

    found = resolve.minimum_wage(MID_2026, sector=cleaning, sector_area=area, job_grade=grade)
    assert found.hourly_rate == Decimal("40.0000")


@pytest.mark.statutory
def test_an_unpriced_grade_falls_back_to_the_area_rate(db, cleaning):
    area = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")
    grade = JobGrade.objects.create(sector=cleaning, code="CLEANER", name="Cleaner")
    wage(sector=cleaning, area=area, hourly="30.0000")

    found = resolve.minimum_wage(MID_2026, sector=cleaning, sector_area=area, job_grade=grade)
    assert found.hourly_rate == Decimal("30.0000")


@pytest.mark.statutory
def test_a_sector_with_no_rate_of_its_own_falls_back_to_the_national_minimum(db, domestic):
    """The NMW is the floor under every sector, so this is a resolution, not an error."""
    wage(hourly="10.0000")
    found = resolve.minimum_wage(MID_2026, sector=domestic)
    assert found.sector is None
    assert found.hourly_rate == Decimal("10.0000")


@pytest.mark.statutory
def test_an_hours_band_falls_back_to_the_all_hours_row(db, domestic):
    wage(sector=domestic, hourly="20.0000")
    found = resolve.minimum_wage(
        MID_2026, sector=domestic, hours_band=MinimumWageRate.HoursBand.LTE_27
    )
    assert found.hourly_rate == Decimal("20.0000")


@pytest.mark.statutory
def test_a_banded_row_is_preferred_over_the_all_hours_row(db, domestic):
    wage(sector=domestic, hourly="20.0000")
    wage(sector=domestic, band=MinimumWageRate.HoursBand.LTE_27, hourly="22.0000")

    found = resolve.minimum_wage(
        MID_2026, sector=domestic, hours_band=MinimumWageRate.HoursBand.LTE_27
    )
    assert found.hourly_rate == Decimal("22.0000")


@pytest.mark.statutory
def test_no_wage_data_at_all_raises(db, domestic):
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.minimum_wage(MID_2026, sector=domestic)


@pytest.mark.statutory
def test_a_superseded_wage_is_still_resolvable_for_its_own_period(db, domestic):
    """A 2026 payroll re-run in 2030 must read the 2026 rate. That is the whole point
    of effective dating, and it is asserted rather than assumed."""
    wage(sector=domestic, hourly="20.0000", frm=MARCH_2026, to=MARCH_2027)
    wage(sector=domestic, hourly="25.0000", frm=MARCH_2027)

    assert resolve.minimum_wage(MID_2026, sector=domestic).hourly_rate == Decimal("20.0000")


# -------------------------------------------------------------- rule set lookup


@pytest.mark.statutory
def test_a_sector_rule_set_wins_over_the_bcea_row(db, domestic):
    rule_set(LeaveRuleSet, sector=None, annual_leave_days_per_cycle_5day=15)
    rule_set(LeaveRuleSet, sector=domestic, annual_leave_days_per_cycle_5day=21)

    found = resolve.leave_rules(domestic, MID_2026)
    assert found.sector == domestic


@pytest.mark.statutory
def test_a_sector_without_its_own_rules_falls_back_to_the_bcea_row(db, domestic):
    rule_set(LeaveRuleSet, sector=None)
    assert resolve.leave_rules(domestic, MID_2026).sector is None


@pytest.mark.statutory
def test_no_rules_at_all_raises(db, domestic):
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.leave_rules(domestic, MID_2026)


@pytest.mark.statutory
def test_the_rule_set_lookup_respects_the_date_boundary(db, domestic):
    rule_set(LeaveRuleSet, sector=domestic, frm=MARCH_2026, to=MARCH_2027)
    rule_set(LeaveRuleSet, sector=None, frm=MARCH_2026)

    assert resolve.leave_rules(domestic, MARCH_2027 - datetime.timedelta(days=1)).sector == domestic
    assert resolve.leave_rules(domestic, MARCH_2027).sector is None


# ------------------------------------------------------------- public holidays


@pytest.mark.statutory
def test_a_public_holiday_is_read_not_computed(db):
    PublicHoliday.objects.create(
        holiday_date=datetime.date(2026, 4, 27),
        name="Freedom Day",
        source_reference=CITATION,
    )
    assert resolve.is_public_holiday(datetime.date(2026, 4, 27)) is True
    assert resolve.is_public_holiday(datetime.date(2026, 4, 26)) is False
    assert resolve.public_holiday_on(datetime.date(2026, 4, 27)).name == "Freedom Day"


@pytest.mark.statutory
def test_holidays_in_a_period_include_both_end_dates(db):
    """Callers pass a pay period, and a pay period's last day is a day people work."""
    for day, name in [
        (datetime.date(2026, 3, 21), "Human Rights Day"),
        (datetime.date(2026, 4, 27), "Freedom Day"),
        (datetime.date(2026, 5, 1), "Workers Day"),
    ]:
        PublicHoliday.objects.create(holiday_date=day, name=name, source_reference=CITATION)

    found = resolve.public_holidays_between(datetime.date(2026, 3, 21), datetime.date(2026, 5, 1))
    assert [h.name for h in found] == ["Human Rights Day", "Freedom Day", "Workers Day"]


# ---------------------------------------------------------------------- PAYE


@pytest.mark.statutory
def test_a_date_with_no_tax_year_raises(db):
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.tax_year(MARCH_2026)


@pytest.mark.statutory
def test_rebate_tiers_are_returned_by_age(db):
    year = TaxYear.objects.create(
        label="2026/2027",
        start_date=MARCH_2026,
        end_date=datetime.date(2027, 2, 28),
    )
    for kind, age in [
        (PayeRebate.RebateType.PRIMARY, 0),
        (PayeRebate.RebateType.SECONDARY, 65),
        (PayeRebate.RebateType.TERTIARY, 75),
    ]:
        PayeRebate.objects.create(
            tax_year=year,
            rebate_type=kind,
            min_age=age,
            annual_amount=Decimal("1000.00"),
            tax_threshold_annual=Decimal("10000.00"),
            source_reference=CITATION,
        )

    assert len(resolve.paye_rebates(year, age=30)) == 1
    assert len(resolve.paye_rebates(year, age=68)) == 2
    assert len(resolve.paye_rebates(year, age=80)) == 3


@pytest.mark.statutory
def test_a_tax_year_with_no_brackets_raises(db):
    year = TaxYear.objects.create(
        label="2026/2027",
        start_date=MARCH_2026,
        end_date=datetime.date(2027, 2, 28),
    )
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.paye_brackets(year)


@pytest.mark.statutory
def test_a_missing_medical_credit_is_the_ordinary_case(db):
    """The one PAYE lookup that returns None rather than raising: most domestic and
    contract cleaning employees have no medical scheme."""
    year = TaxYear.objects.create(
        label="2026/2027",
        start_date=MARCH_2026,
        end_date=datetime.date(2027, 2, 28),
    )
    assert resolve.medical_tax_credit(year) is None
