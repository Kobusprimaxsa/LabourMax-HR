"""The arithmetic checks, and proof that they bite.

A consistency check that has never failed is indistinguishable from one that cannot
fail. Each test here breaks exactly one digit of an otherwise correct table and
asserts the check catches it — which is the only way to know the check would catch
the same mistake in a real load.

The figures below are structurally shaped like a real PAYE table but are not one.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from statutory import checks
from statutory.models import MinimumWageRate, PayeRebate, PayeTaxBracket, TaxYear

CITATION = "Test fixture, not a real SARS table"

# Bands chosen so every base reconciles: 10% of 100 000 is 10 000, and
# 10 000 + 20% of 100 000 is 30 000.
BANDS = [
    (1, "0.00", "100000.00", "0.00", "10.000"),
    (2, "100000.00", "200000.00", "10000.00", "20.000"),
    (3, "200000.00", None, "30000.00", "30.000"),
]


@pytest.fixture
def year(db):
    year = TaxYear.objects.create(
        label="2026/2027",
        start_date=datetime.date(2026, 3, 1),
        end_date=datetime.date(2027, 2, 28),
    )
    for order, low, high, base, rate in BANDS:
        PayeTaxBracket.objects.create(
            tax_year=year,
            bracket_order=order,
            income_from=Decimal(low),
            income_to=None if high is None else Decimal(high),
            base_tax=Decimal(base),
            marginal_rate_pct=Decimal(rate),
            source_reference=CITATION,
        )
    # 10% is the lowest band's rate, so a R5 000 rebate cancels the tax at R50 000.
    PayeRebate.objects.create(
        tax_year=year,
        rebate_type=PayeRebate.RebateType.PRIMARY,
        min_age=0,
        annual_amount=Decimal("5000.00"),
        tax_threshold_annual=Decimal("50000.00"),
        source_reference=CITATION,
    )
    return year


@pytest.mark.statutory
def test_a_consistent_table_reports_nothing(year):
    assert checks.check_paye_brackets(year) == []
    assert checks.check_paye_rebates(year) == []


@pytest.mark.statutory
def test_a_mistyped_base_is_caught(year):
    """One digit. The kind of error proofreading misses and arithmetic does not."""
    band = year.brackets.get(bracket_order=2)
    band.base_tax = Decimal("10100.00")
    band.save()

    issues = checks.check_paye_brackets(year)
    assert issues and all(issue.blocking for issue in issues)
    assert "10100" in str(issues[0])


@pytest.mark.statutory
def test_a_mistyped_marginal_rate_is_caught_through_the_band_above_it(year):
    """A wrong rate does not show up in its own row — it shows up in the next base."""
    band = year.brackets.get(bracket_order=1)
    band.marginal_rate_pct = Decimal("11.000")
    band.save()

    issues = checks.check_paye_brackets(year)
    assert any("band 2" in issue.where for issue in issues)


@pytest.mark.statutory
def test_a_gap_between_bands_is_caught(year):
    """An income landing in a gap resolves to no band at all, mid payroll run."""
    band = year.brackets.get(bracket_order=2)
    band.income_from = Decimal("100001.00")
    band.save()

    issues = checks.check_paye_brackets(year)
    assert any("touch exactly" in issue.message for issue in issues)


@pytest.mark.statutory
def test_a_first_band_that_does_not_start_at_zero_is_caught(year):
    band = year.brackets.get(bracket_order=1)
    band.income_from = Decimal("1.00")
    band.save()

    issues = checks.check_paye_brackets(year)
    assert any("not 0" in issue.message for issue in issues)


@pytest.mark.statutory
def test_a_top_band_with_a_ceiling_is_caught(year):
    band = year.brackets.get(bracket_order=3)
    band.income_to = Decimal("5000000.00")
    band.save()

    issues = checks.check_paye_brackets(year)
    assert any("open-ended" in issue.message for issue in issues)


@pytest.mark.statutory
def test_a_threshold_that_disagrees_with_its_rebate_is_caught(year):
    rebate = year.rebates.get()
    rebate.tax_threshold_annual = Decimal("51000.00")
    rebate.save()

    issues = checks.check_paye_rebates(year)
    assert issues and issues[0].blocking


@pytest.mark.statutory
def test_sub_rand_rounding_in_a_threshold_is_tolerated(year):
    """SARS publishes whole-rand thresholds. A few cents is their rounding, not an error."""
    rebate = year.rebates.get()
    rebate.tax_threshold_annual = Decimal("50000.40")
    rebate.save()

    assert checks.check_paye_rebates(year) == []


@pytest.mark.statutory
def test_a_year_with_no_brackets_is_blocking(db):
    year = TaxYear.objects.create(
        label="2027/2028",
        start_date=datetime.date(2027, 3, 1),
        end_date=datetime.date(2028, 2, 29),
    )
    issues = checks.check_paye_brackets(year)
    assert issues and issues[0].blocking


# ----------------------------------------------------------------- wage rounding


@pytest.mark.statutory
def test_gazetted_rounding_in_a_weekly_rate_is_only_a_warning(db):
    """Where the gazette's own figures disagree by cents, the gazette wins."""
    MinimumWageRate.objects.create(
        hourly_rate=Decimal("30.2300"),
        weekly_rate_45h=Decimal("1360.00"),
        effective_from=datetime.date(2026, 3, 1),
        source_reference=CITATION,
    )
    issues = checks.check_gazetted_wage_derivations()
    assert all(not issue.blocking for issue in issues)


@pytest.mark.statutory
def test_a_weekly_rate_from_the_wrong_row_is_reported(db):
    MinimumWageRate.objects.create(
        hourly_rate=Decimal("30.2300"),
        weekly_rate_45h=Decimal("1497.15"),
        effective_from=datetime.date(2026, 3, 1),
        source_reference=CITATION,
    )
    issues = checks.check_gazetted_wage_derivations()
    assert issues, "A weekly rate belonging to a different area must be reported."


# -------------------------------------------------------------------- citations


@pytest.mark.statutory
def test_a_placeholder_citation_is_caught(db):
    """The CHECK constraint refuses a blank citation. It cannot refuse 'TBC'."""
    MinimumWageRate.objects.create(
        hourly_rate=Decimal("30.2300"),
        effective_from=datetime.date(2026, 3, 1),
        source_reference="TBC",
    )
    issues = checks.check_citations()
    assert issues and issues[0].blocking
