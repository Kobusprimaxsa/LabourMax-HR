"""The minimum wage table, and the two constraints that make it trustworthy.

A rate table with overlapping periods returns a different answer depending on row
order. That is the least debuggable class of payroll bug: nothing errors, every
payslip looks plausible, and two employers in the same sector are paid differently
because their rows were inserted in a different sequence.

So overlap is impossible at the database level, not merely discouraged in a form.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from statutory.models import JobGrade, MinimumWageRate, Sector, SectorArea

MARCH_2026 = datetime.date(2026, 3, 1)
MARCH_2027 = datetime.date(2027, 3, 1)

CITATION = "Test fixture, not a real gazette"


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING,
        name="Contract cleaning sector",
        determination_reference="Sectoral Determination 1",
        uses_area_rates=True,
        has_statutory_annual_bonus=True,
    )


@pytest.fixture
def domestic(db):
    return Sector.objects.create(
        code=Sector.Code.DOMESTIC,
        name="Domestic worker sector",
        determination_reference="Sectoral Determination 7",
    )


def rate(
    *,
    sector=None,
    area=None,
    grade=None,
    hourly="30.0000",
    frm=MARCH_2026,
    to=None,
    band=None,
):
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


# ------------------------------------------------------------------- the citation


@pytest.mark.statutory
def test_a_rate_without_a_citation_is_refused(db, domestic):
    """The discipline of the whole phase, in one assertion."""
    with pytest.raises(IntegrityError), transaction.atomic():
        MinimumWageRate.objects.create(
            sector=domestic,
            hourly_rate=Decimal("30.0000"),
            effective_from=MARCH_2026,
            source_reference="",
        )


@pytest.mark.statutory
def test_a_citation_need_not_be_a_gazette(db, cleaning):
    """KwaZulu-Natal contract cleaning rates come from a collective agreement.

    A column called gazette_reference would have forced a lie here.
    """
    area = SectorArea.objects.create(
        sector=cleaning,
        code="AREA_B_KZN",
        name="KwaZulu-Natal",
        uses_bargaining_council_rates=True,
    )
    row = rate(sector=cleaning, area=area, hourly="31.0000")
    row.source_reference = "BCCCI Collective Agreement 2026, clause 8"
    row.save()
    assert row.pk


# --------------------------------------------------------------------- the ranges


@pytest.mark.statutory
def test_a_zero_or_negative_rate_is_refused(db, domestic):
    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=domestic, hourly="0.0000")


@pytest.mark.statutory
def test_an_inverted_date_range_is_refused(db, domestic):
    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=domestic, frm=MARCH_2027, to=MARCH_2026)


@pytest.mark.statutory
def test_consecutive_periods_are_allowed(db, domestic):
    """effective_to is EXCLUSIVE, so one period may end the day the next begins.

    If this failed, every annual rate change would need a one-day gap, and a
    payroll run covering that day would find no rate at all.
    """
    rate(sector=domestic, frm=MARCH_2026, to=MARCH_2027, hourly="30.0000")
    later = rate(sector=domestic, frm=MARCH_2027, hourly="32.0000")
    assert later.pk
    assert MinimumWageRate.objects.filter(sector=domestic).count() == 2


@pytest.mark.statutory
def test_overlapping_periods_for_the_same_scope_are_refused(db, domestic):
    rate(sector=domestic, frm=MARCH_2026, to=MARCH_2027)
    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=domestic, frm=datetime.date(2026, 9, 1), to=datetime.date(2027, 6, 1))


@pytest.mark.statutory
def test_an_open_ended_period_blocks_any_later_row_for_the_same_scope(db, domestic):
    """The most likely real mistake: loading next year's rate without closing this year's."""
    rate(sector=domestic, frm=MARCH_2026, to=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=domestic, frm=MARCH_2027, to=None)


@pytest.mark.statutory
def test_different_scopes_may_share_a_period(db, domestic, cleaning):
    """Overlap is only forbidden within one scope. Two sectors are two scopes."""
    rate(sector=domestic, frm=MARCH_2026)
    other = rate(sector=cleaning, frm=MARCH_2026, hourly="33.0000")
    assert other.pk


@pytest.mark.statutory
def test_areas_and_grades_are_separate_scopes(db, cleaning):
    area_a = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")
    area_c = SectorArea.objects.create(sector=cleaning, code="AREA_C", name="Area C")
    supervisor = JobGrade.objects.create(sector=cleaning, code="SUPERVISOR", name="Supervisor")

    assert rate(sector=cleaning, area=area_a, hourly="33.0000").pk
    assert rate(sector=cleaning, area=area_c, hourly="30.0000").pk
    assert rate(sector=cleaning, area=area_a, grade=supervisor, hourly="36.0000").pk

    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=cleaning, area=area_a, hourly="34.0000")


@pytest.mark.statutory
def test_the_hours_band_is_a_separate_scope(db, domestic):
    """The SD7 split between 27-hour weeks and longer."""
    assert rate(sector=domestic, band=MinimumWageRate.HoursBand.LTE_27, hourly="33.0000").pk
    assert rate(sector=domestic, band=MinimumWageRate.HoursBand.GT_27, hourly="30.0000").pk


# ------------------------------------------- the NULL-is-not-NULL trap, asserted


@pytest.mark.statutory
def test_two_national_minimum_wage_rows_cannot_share_a_date(db):
    """The rows most likely to be loaded twice are the ones NULL would have exposed.

    All three scope columns are NULL for the general National Minimum Wage. In
    PostgreSQL NULL is not equal to NULL, so an ordinary unique constraint would
    have permitted a duplicate, and an exclusion constraint without COALESCE would
    have permitted an overlap. Both are handled; this asserts it.
    """
    rate(sector=None, frm=MARCH_2026, hourly="30.2300")

    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=None, frm=MARCH_2026, hourly="99.0000")


@pytest.mark.statutory
def test_overlapping_national_minimum_wage_periods_are_refused(db):
    rate(sector=None, frm=MARCH_2026, to=MARCH_2027, hourly="30.2300")

    with pytest.raises(IntegrityError), transaction.atomic():
        rate(sector=None, frm=datetime.date(2026, 6, 1), to=MARCH_2027, hourly="31.0000")


@pytest.mark.statutory
def test_the_national_minimum_wage_is_a_different_scope_from_a_sector(db, domestic):
    assert rate(sector=None, frm=MARCH_2026, hourly="30.2300").pk
    assert rate(sector=domestic, frm=MARCH_2026, hourly="30.2300").pk


# ---------------------------------------------------------- gazetted derived rates


@pytest.mark.statutory
def test_gazetted_derived_rates_are_stored_as_published(db, domestic):
    """Not recomputed. Gazettes round, and a two-cent disagreement is a dispute."""
    row = MinimumWageRate.objects.create(
        sector=domestic,
        hourly_rate=Decimal("30.2300"),
        weekly_rate_45h=Decimal("1360.35"),
        monthly_rate_45h=Decimal("5895.00"),
        daily_rate_9h=Decimal("272.07"),
        effective_from=MARCH_2026,
        source_reference=CITATION,
    )
    row.refresh_from_db()
    assert row.weekly_rate_45h == Decimal("1360.35")
    assert isinstance(row.hourly_rate, Decimal), "Money must never round-trip as a float."
