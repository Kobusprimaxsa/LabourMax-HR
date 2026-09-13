"""``statutory_parameter`` — every scalar figure that is not a wage or a bracket.

One table, one row per figure per period, because these move on different calendars:
the UIF ceiling on ministerial notice with no fixed date, the BCEA threshold usually
in April, SDL and COIDA on their own schedules. A column per figure would put a
migration in the path of every rate change, which is the thing this phase exists to
prevent.

The constraint that matters most is the overlap exclusion. Two rows for
UIF_MONTHLY_CEILING covering the same day give a different answer depending on row
order, and the wrong one is indistinguishable from the right one on a payslip.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from statutory.models import StatutoryParameter

MARCH_2026 = datetime.date(2026, 3, 1)
MARCH_2027 = datetime.date(2027, 3, 1)

CITATION = "Test fixture, not a real notice"

CODE = "TEST_PARAMETER"
OTHER_CODE = "TEST_OTHER_PARAMETER"


def parameter(*, code=CODE, value="1.000000", text="", frm=MARCH_2026, to=None, unit=None):
    return StatutoryParameter.objects.create(
        parameter_code=code,
        value_numeric=None if value is None else Decimal(value),
        value_text=text,
        unit=unit or StatutoryParameter.Unit.ZAR,
        effective_from=frm,
        effective_to=to,
        source_reference=CITATION,
    )


@pytest.mark.statutory
def test_a_parameter_without_a_citation_is_refused(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        StatutoryParameter.objects.create(
            parameter_code=CODE,
            value_numeric=Decimal("1.000000"),
            effective_from=MARCH_2026,
            source_reference="",
        )


@pytest.mark.statutory
def test_a_parameter_with_no_value_at_all_is_refused(db):
    """A row with neither a number nor text is a placeholder somebody meant to fill in.

    Left permitted, it reads as loaded data and resolves to nothing at run time.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        parameter(value=None, text="")


@pytest.mark.statutory
def test_a_text_only_parameter_is_allowed(db):
    """For the rare non-numeric figure. The numeric column stays NULL rather than 0."""
    row = parameter(value=None, text="forward-looking employer declaration")
    assert row.pk
    assert row.value_numeric is None


@pytest.mark.statutory
def test_the_value_is_a_decimal_and_stays_one(db):
    """NUMERIC(16,6), never a float. A 1% rate stored as a float is not 1%."""
    row = parameter(value="0.010000", unit=StatutoryParameter.Unit.PERCENT)
    row.refresh_from_db()
    assert isinstance(row.value_numeric, Decimal)
    assert row.value_numeric == Decimal("0.010000")


@pytest.mark.statutory
def test_six_decimal_places_survive_the_round_trip(db):
    row = parameter(value="0.123456")
    row.refresh_from_db()
    assert row.value_numeric == Decimal("0.123456")


@pytest.mark.statutory
def test_an_inverted_effective_range_is_refused(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        parameter(frm=MARCH_2027, to=MARCH_2026)


@pytest.mark.statutory
def test_the_same_code_cannot_start_twice_on_one_date(db):
    parameter(frm=MARCH_2026, to=MARCH_2027)
    with pytest.raises(IntegrityError), transaction.atomic():
        parameter(frm=MARCH_2026, to=MARCH_2027, value="2.000000")


@pytest.mark.statutory
def test_overlapping_periods_for_one_code_are_refused(db):
    parameter(frm=MARCH_2026, to=MARCH_2027)
    with pytest.raises(IntegrityError), transaction.atomic():
        parameter(frm=datetime.date(2026, 9, 1), to=datetime.date(2027, 9, 1))


@pytest.mark.statutory
def test_loading_a_new_value_without_closing_the_old_one_is_refused(db):
    """The realistic mistake: the UIF ceiling changes, the new row is inserted, and
    the old open-ended row is left open. Both then apply."""
    parameter(frm=MARCH_2026, to=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        parameter(frm=MARCH_2027, to=None, value="2.000000")


@pytest.mark.statutory
def test_consecutive_periods_are_allowed(db):
    """effective_to is exclusive, so a period may end the day the next begins."""
    parameter(frm=MARCH_2026, to=MARCH_2027)
    assert parameter(frm=MARCH_2027, to=None, value="2.000000").pk


@pytest.mark.statutory
def test_different_codes_are_different_scopes(db):
    parameter(code=CODE, frm=MARCH_2026)
    assert parameter(code=OTHER_CODE, frm=MARCH_2026).pk
