"""The fixture that ships with the repo, loaded and checked on every commit.

Everything else in this app tests the machinery. This tests the data: the actual
1 March 2026 figures, loaded through the actual loader, checked by the actual
arithmetic. If someone edits a digit in ``reference/ref-2026.03.01.json``, this is
what notices.

It is deliberately not a golden test. A golden test reproduces a published worked
example and proves the figures are *right*; this proves the file loads and hangs
together. The golden tests arrive with the calculators in P7, and they are what
finally lets a reference version be marked ``golden_tests_passed``.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import checks, resolve
from statutory.loader import load_reference_data
from statutory.models import MinimumWageRate, PublicHoliday, Sector

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "reference" / "ref-2026.03.01.json"

MARCH_2026 = datetime.date(2026, 3, 1)


@pytest.fixture
def loaded(db):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


@pytest.mark.statutory
def test_the_shipped_fixture_exists():
    assert FIXTURE.exists(), f"{FIXTURE} is missing - the reference data ships with the repo."


@pytest.mark.statutory
def test_the_shipped_fixture_loads(loaded):
    assert loaded.total_created > 0
    assert not loaded.closed_periods, "A first load has nothing to supersede."


@pytest.mark.statutory
def test_the_shipped_fixture_reconciles(loaded):
    blocking = [issue for issue in checks.run_all() if issue.blocking]
    assert not blocking, "\n".join(str(issue) for issue in blocking)


@pytest.mark.statutory
def test_every_loaded_rate_carries_a_real_citation(loaded):
    for rate in MinimumWageRate.objects.all():
        assert len(rate.source_reference) > 20, (
            f"{rate} is cited as {rate.source_reference!r}, which is too short to find "
            f"the source again."
        )


@pytest.mark.statutory
def test_the_national_minimum_wage_resolves_on_the_day_it_takes_effect(loaded):
    rate = resolve.minimum_wage(MARCH_2026)
    assert rate.sector is None
    assert rate.hourly_rate == Decimal("30.2300")


@pytest.mark.statutory
def test_a_domestic_employer_resolves_to_the_domestic_row_not_the_fallback(loaded):
    domestic = Sector.objects.get(code=Sector.Code.DOMESTIC)
    rate = resolve.minimum_wage(MARCH_2026, sector=domestic)
    assert rate.sector == domestic


@pytest.mark.statutory
def test_the_day_before_the_gazette_takes_effect_resolves_to_nothing(loaded):
    """There is no earlier row loaded, and the system says so rather than guessing."""
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.minimum_wage(MARCH_2026 - datetime.timedelta(days=1))


@pytest.mark.statutory
def test_kwazulu_natal_contract_cleaning_has_no_rate_and_falls_back_visibly(loaded):
    """The gazette gives Area C no figure, so none was invented.

    A KwaZulu-Natal employer therefore resolves to the sector-wide fallback, which is
    wrong for them — the BCCCI agreement governs. That is why the area is flagged
    ``uses_bargaining_council_rates`` and why the watch list carries it as not loaded:
    onboarding must refuse a KwaZulu-Natal contract cleaning employer until the
    agreement is in hand.
    """
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    kzn = cleaning.areas.get(code="AREA_C_KZN")

    assert kzn.uses_bargaining_council_rates is True
    assert not MinimumWageRate.objects.filter(sector_area=kzn).exists()


@pytest.mark.statutory
def test_the_bcea_threshold_changes_on_the_first_of_may(loaded):
    """Two rows, because the threshold moved inside this reference version's life."""
    april = resolve.parameter_value("BCEA_EARNINGS_THRESHOLD", datetime.date(2026, 4, 30))
    may = resolve.parameter_value("BCEA_EARNINGS_THRESHOLD", datetime.date(2026, 5, 1))

    assert april == Decimal("261748.450000")
    assert may == Decimal("269600.900000")


@pytest.mark.statutory
def test_the_uif_ceiling_and_rates_resolve(loaded):
    assert resolve.parameter_value("UIF_MONTHLY_CEILING", MARCH_2026) == Decimal("17712.000000")
    assert resolve.parameter_value("UIF_EMPLOYEE_RATE_PCT", MARCH_2026) == Decimal("1.000000")
    assert resolve.parameter_value("UIF_EMPLOYER_RATE_PCT", MARCH_2026) == Decimal("1.000000")


@pytest.mark.statutory
def test_the_2026_womens_day_shift_is_stored_as_the_monday(loaded):
    """9 August 2026 is a Sunday, so the holiday is the 10th — stored, not computed."""
    monday = PublicHoliday.objects.get(holiday_date=datetime.date(2026, 8, 10))
    assert monday.shifted_from_date == datetime.date(2026, 8, 9)
    assert resolve.is_public_holiday(datetime.date(2026, 8, 9)) is False
    assert resolve.is_public_holiday(datetime.date(2026, 8, 10)) is True


@pytest.mark.statutory
def test_both_calendar_years_are_loaded_in_full(loaded):
    for year in (2026, 2027):
        count = PublicHoliday.objects.filter(
            holiday_date__year=year,
        ).count()
        assert count == 12, f"{year} has {count} public holidays loaded, expected 12."


@pytest.mark.statutory
def test_the_tax_year_covers_its_own_dates(loaded):
    year = resolve.tax_year(MARCH_2026)
    assert year.label == "2026/2027"
    assert len(resolve.paye_brackets(year)) == 7
    assert len(resolve.paye_rebates(year, age=68)) == 2


@pytest.mark.statutory
def test_the_version_is_loaded_but_not_in_force(loaded):
    """Nothing can run payroll against this until a person has checked every figure."""
    from statutory.models import ReferenceDataVersion

    version = ReferenceDataVersion.objects.get(version_label="REF-2026.03.01")
    assert version.verified_at is None
    assert version.is_usable is False
    assert ReferenceDataVersion.in_force_on(MARCH_2026) is None
