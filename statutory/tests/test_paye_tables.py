"""The PAYE tables: tax years, brackets, rebates and medical credits.

SARS publishes the brackets as "R X of taxable income above R Y, plus R Z", and they
are stored in exactly that shape so the calculator is a transcription of the
published table rather than an interpretation of it. What these tests protect is the
*shape*: bounds that cannot invert, band numbers that cannot repeat within a year,
and a rebate tier that cannot be loaded twice.

No statutory figure appears in this file. The numbers are placeholders chosen to be
obviously not real — the real ones live in the reference data load, with citations.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from statutory.models import MedicalTaxCreditRate, PayeRebate, PayeTaxBracket, TaxYear

CITATION = "Test fixture, not a real SARS table"


@pytest.fixture
def year(db):
    return TaxYear.objects.create(
        label="2026/2027",
        start_date=datetime.date(2026, 3, 1),
        end_date=datetime.date(2027, 2, 28),
    )


def bracket(year, *, order=1, frm="0.00", to="100000.00", base="0.00", rate="10.000"):
    return PayeTaxBracket.objects.create(
        tax_year=year,
        bracket_order=order,
        income_from=Decimal(frm),
        income_to=None if to is None else Decimal(to),
        base_tax=Decimal(base),
        marginal_rate_pct=Decimal(rate),
        source_reference=CITATION,
    )


# --------------------------------------------------------------------- tax years


@pytest.mark.statutory
def test_a_tax_year_cannot_end_before_it_starts(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        TaxYear.objects.create(
            label="Backwards",
            start_date=datetime.date(2027, 2, 28),
            end_date=datetime.date(2026, 3, 1),
        )


@pytest.mark.statutory
def test_two_tax_years_cannot_share_a_start_date(db, year):
    """1 March identifies the year. Two rows for one year is two sets of brackets."""
    with pytest.raises(IntegrityError), transaction.atomic():
        TaxYear.objects.create(
            label="dup 2026/27",
            start_date=datetime.date(2026, 3, 1),
            end_date=datetime.date(2027, 2, 28),
        )


@pytest.mark.statutory
def test_for_date_finds_the_year_containing_a_date(db, year):
    assert TaxYear.for_date(datetime.date(2026, 3, 1)) == year
    assert TaxYear.for_date(datetime.date(2027, 2, 28)) == year
    assert TaxYear.for_date(datetime.date(2027, 3, 1)) is None


@pytest.mark.statutory
def test_a_tax_year_is_inclusive_of_both_ends(db, year):
    """Unlike every effective-dated table here, a tax year is closed at both ends.

    The distinction is deliberate and it is the one most likely to be "tidied" into
    consistency: 28 February is inside the year, whereas an effective_to of
    28 February would mean the row stopped applying that morning.
    """
    assert TaxYear.for_date(year.end_date) == year


# ---------------------------------------------------------------------- brackets


@pytest.mark.statutory
def test_a_bracket_without_a_citation_is_refused(db, year):
    with pytest.raises(IntegrityError), transaction.atomic():
        PayeTaxBracket.objects.create(
            tax_year=year,
            bracket_order=1,
            income_from=Decimal("0.00"),
            income_to=Decimal("100000.00"),
            base_tax=Decimal("0.00"),
            marginal_rate_pct=Decimal("10.000"),
            source_reference="",
        )


@pytest.mark.statutory
def test_a_band_number_cannot_repeat_within_a_tax_year(db, year):
    """Two band 3s is a transcription error, and it changes the answer silently."""
    bracket(year, order=3)
    with pytest.raises(IntegrityError), transaction.atomic():
        bracket(year, order=3, frm="200000.00", to="300000.00")


@pytest.mark.statutory
def test_an_inverted_income_range_is_refused(db, year):
    with pytest.raises(IntegrityError), transaction.atomic():
        bracket(year, frm="300000.00", to="100000.00")


@pytest.mark.statutory
def test_the_top_bracket_has_no_upper_bound(db, year):
    """NULL income_to is the top band, and it must be allowed to have no ceiling."""
    top = bracket(year, order=9, frm="2000000.00", to=None, rate="45.000")
    assert top.pk
    assert top.income_to is None


@pytest.mark.statutory
def test_a_marginal_rate_outside_nought_to_a_hundred_is_refused(db, year):
    """A rate stored as 0.26 instead of 26.000 is the classic transcription slip.

    This CHECK does not catch that one — 0.26 is a valid percentage — but it does
    catch the same mistake made in the other direction, and it makes the column's
    unit unambiguous to anyone reading the SQL. NUMERIC(6,3) stops anything above
    999.999 before the CHECK is even reached, so the CHECK is what covers the band
    between 100 and 1000.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        bracket(year, rate="100.001")

    with pytest.raises(IntegrityError), transaction.atomic():
        bracket(year, order=2, rate="-1.000")


@pytest.mark.statutory
def test_negative_amounts_are_refused(db, year):
    with pytest.raises(IntegrityError), transaction.atomic():
        bracket(year, base="-100.00")


@pytest.mark.statutory
def test_brackets_come_back_lowest_band_first(db, year):
    bracket(year, order=3, frm="200000.00", to="300000.00")
    bracket(year, order=1, frm="0.00", to="100000.00")
    bracket(year, order=2, frm="100000.00", to="200000.00")

    assert [b.bracket_order for b in year.brackets.all()] == [1, 2, 3]


@pytest.mark.statutory
def test_a_tax_year_in_use_cannot_be_deleted(db, year):
    """PROTECT. Deleting a year out from under its brackets loses the history.

    A closed year still has to reprint payslips and IRP5s for as long as SARS can
    ask about them.
    """
    bracket(year)
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        year.delete()


# ----------------------------------------------------------------------- rebates


@pytest.mark.statutory
def test_a_rebate_tier_cannot_be_loaded_twice_for_a_year(db, year):
    PayeRebate.objects.create(
        tax_year=year,
        rebate_type=PayeRebate.RebateType.PRIMARY,
        min_age=0,
        annual_amount=Decimal("1000.00"),
        tax_threshold_annual=Decimal("10000.00"),
        source_reference=CITATION,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PayeRebate.objects.create(
            tax_year=year,
            rebate_type=PayeRebate.RebateType.PRIMARY,
            min_age=0,
            annual_amount=Decimal("2000.00"),
            tax_threshold_annual=Decimal("20000.00"),
            source_reference=CITATION,
        )


@pytest.mark.statutory
def test_the_three_tiers_coexist_and_are_stored_per_tier(db, year):
    """Per tier, not pre-summed. A 68-year-old gets primary plus secondary, and the
    calculator does that addition — one stored total would be a number to get wrong
    on somebody's birthday."""
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

    assert year.rebates.count() == 3
    assert [r.min_age for r in year.rebates.all()] == [0, 65, 75]


@pytest.mark.statutory
def test_a_rebate_without_a_citation_is_refused(db, year):
    with pytest.raises(IntegrityError), transaction.atomic():
        PayeRebate.objects.create(
            tax_year=year,
            rebate_type=PayeRebate.RebateType.PRIMARY,
            min_age=0,
            annual_amount=Decimal("1000.00"),
            tax_threshold_annual=Decimal("10000.00"),
            source_reference="",
        )


# --------------------------------------------------------------- medical credits


@pytest.mark.statutory
def test_one_set_of_medical_credits_per_tax_year(db, year):
    MedicalTaxCreditRate.objects.create(
        tax_year=year,
        main_member_monthly=Decimal("100.00"),
        first_dependant_monthly=Decimal("100.00"),
        additional_dependant_monthly=Decimal("50.00"),
        source_reference=CITATION,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MedicalTaxCreditRate.objects.create(
            tax_year=year,
            main_member_monthly=Decimal("200.00"),
            first_dependant_monthly=Decimal("200.00"),
            additional_dependant_monthly=Decimal("100.00"),
            source_reference=CITATION,
        )


@pytest.mark.statutory
def test_negative_medical_credits_are_refused(db, year):
    with pytest.raises(IntegrityError), transaction.atomic():
        MedicalTaxCreditRate.objects.create(
            tax_year=year,
            main_member_monthly=Decimal("-1.00"),
            first_dependant_monthly=Decimal("100.00"),
            additional_dependant_monthly=Decimal("50.00"),
            source_reference=CITATION,
        )
