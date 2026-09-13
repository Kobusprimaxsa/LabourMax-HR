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


# ----------------------------------------------------------------- source codes


def source_code(**overrides):
    from statutory.models import SarsSourceCode

    values = {
        "code": "3601",
        "description": "Income (Subject to PAYE)",
        "code_group": SarsSourceCode.Group.INCOME,
        "is_taxable": True,
        "is_uif_remuneration": True,
        "is_sdl_remuneration": True,
        "is_coida_remuneration": True,
        "source_reference": CITATION,
    }
    values.update(overrides)
    return SarsSourceCode.objects.create(**values)


@pytest.mark.statutory
def test_a_consistent_source_code_reports_nothing(db):
    source_code()
    assert checks.check_source_codes() == []


@pytest.mark.statutory
def test_a_non_taxable_code_in_the_uif_base_is_caught(db):
    """Both bases are built on the Fourth Schedule definition. A code outside it
    cannot be inside them."""
    source_code(code="3602", is_taxable=False, is_uif_remuneration=True)
    issues = checks.check_source_codes()
    assert issues and issues[0].blocking


@pytest.mark.statutory
def test_a_total_carrying_a_base_flag_is_caught(db):
    """UIF charged on the UIF figure is what this prevents."""
    from statutory.models import SarsSourceCode

    source_code(code="4141", code_group=SarsSourceCode.Group.DEDUCTION, is_taxable=False)
    issues = checks.check_source_codes()
    assert issues and any("calculated FROM" in issue.message for issue in issues)


@pytest.mark.statutory
def test_commission_is_a_valid_shape(db):
    """Taxable, in the SDL base, out of the UIF base - the row this table exists for,
    and no check may object to it."""
    source_code(
        code="3606",
        description="Commission (Subject to PAYE)",
        is_taxable=True,
        is_uif_remuneration=False,
        is_sdl_remuneration=True,
        is_coida_remuneration=True,
    )
    assert checks.check_source_codes() == []


# ----------------------------------------------------------------------- banks


@pytest.mark.statutory
def test_a_six_digit_branch_code_reports_nothing(db):
    from statutory.models import Bank

    Bank.objects.create(name="Test Bank", universal_branch_code="632005")
    assert checks.check_banks() == []


@pytest.mark.statutory
def test_a_transposed_branch_code_is_reported_as_a_warning(db):
    """Five digits instead of six. Surfaces as a returned payment otherwise."""
    from statutory.models import Bank

    Bank.objects.create(name="Typo Bank", universal_branch_code="63205")
    issues = checks.check_banks()
    assert issues and not issues[0].blocking


@pytest.mark.statutory
def test_a_bank_with_no_known_account_length_is_allowed(db):
    """NULL means nobody has confirmed the rule, and that is a legitimate state."""
    from statutory.models import Bank

    bank = Bank.objects.create(name="Unknown Length Bank", universal_branch_code="470010")
    assert bank.account_number_min_length is None
    assert bank.account_number_max_length is None


@pytest.mark.statutory
def test_one_account_length_bound_without_the_other_is_refused(db):
    """A half-loaded rule validates nothing while looking as though it does."""
    from django.db import IntegrityError, transaction

    from statutory.models import Bank

    with pytest.raises(IntegrityError), transaction.atomic():
        Bank.objects.create(
            name="Half Loaded Bank",
            universal_branch_code="198765",
            account_number_min_length=9,
        )
