"""The night allowance is a PAIR too, and a checked enum (O-22).

``night_allowance_value = 0.0000`` stood in for "this instrument states no
amount" — BCEA s17(2)(a)'s actual position, since it requires an allowance
"which may be a shift allowance, or by a reduction of working hours" and sets no
figure. A type column existed to tell that apart from a real zero, which is why
D-113 accepted the sentinel at the time; but that column carried no choices and
no CHECK, so it could hold any string at all, ``percentage`` beside a zero
included. O-22 set the deadline for fixing it at the start of P7, because the
moment ``calculators/gross.py`` multiplies by that zero it pays nothing.

Constraints are asserted from ``pg_constraint`` rather than from the model: a
CheckConstraint declared on an abstract base is NOT inherited by a child that
declares its own Meta, and every model here declares one for ``db_table``.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from statutory import resolve
from statutory.models import NightAllowanceType, Sector, WorkingTimeRuleSet
from statutory.resolve import NightAllowanceState

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

TABLE = "working_time_rule_set"
PAIR_CONSTRAINTS = {
    "working_time_night_allowance_type_is_known",
    "working_time_night_allowance_states_its_figure",
    "working_time_night_allowance_without_a_figure_is_null",
}


def _rules(
    *, sector=None, sector_area=None, kind="by_agreement", value=None, source="Test fixture"
):
    return WorkingTimeRuleSet.objects.create(
        sector=sector,
        sector_area=sector_area,
        effective_from=datetime.date(1997, 12, 1),
        source_reference=source,
        ordinary_hours_per_week=Decimal("45"),
        ordinary_hours_per_day_5day=Decimal("9"),
        ordinary_hours_per_day_6day=Decimal("8"),
        overtime_multiplier=Decimal("1.5"),
        max_overtime_hours_per_day=Decimal("3"),
        max_overtime_hours_per_week=Decimal("10"),
        sunday_multiplier_ordinary=Decimal("1.5"),
        sunday_multiplier_non_ordinary=Decimal("2.0"),
        public_holiday_worked_multiplier=Decimal("2.0"),
        public_holiday_not_worked_paid=True,
        night_work_start_time=datetime.time(18, 0),
        night_work_end_time=datetime.time(6, 0),
        night_allowance_type=kind,
        night_allowance_value=None if value is None else Decimal(value),
        standby_allowance_per_shift=Decimal("50.00"),
        standby_window_start=datetime.time(18, 0),
        standby_window_end=datetime.time(6, 0),
        standby_hours_before_overtime=Decimal("2"),
        min_paid_hours_per_day=Decimal("6"),
        meal_interval_after_hours=Decimal("5"),
        meal_interval_minutes=60,
        daily_rest_hours=12,
        weekly_rest_hours=36,
        accommodation_deduction_capped=False,
        accommodation_deduction_max_pct=None,
    )


def test_all_three_constraints_exist_in_the_database():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'",
            [TABLE],
        )
        landed = {row[0] for row in cursor.fetchall()}
    assert PAIR_CONSTRAINTS <= landed, f"missing: {sorted(PAIR_CONSTRAINTS - landed)}"


# ------------------------------------------------- watch each of them refuse


def test_a_type_that_promises_a_figure_and_carries_none_is_refused():
    """The row this whole change exists for: the calculator would pay nothing
    for night work and nothing would say so."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _rules(kind="percentage", value=None)

    assert "working_time_night_allowance_states_its_figure" in str(raised.value)


def test_a_type_that_names_no_figure_and_carries_one_anyway_is_refused():
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _rules(kind="by_agreement", value="10")

    assert "working_time_night_allowance_without_a_figure_is_null" in str(raised.value)


def test_a_type_outside_the_four_is_refused_by_the_database_not_only_by_choices():
    """``choices`` is a form-layer opinion that raw SQL walks straight past. The
    Conventions table has always required a CHECK beside it; this column had
    neither until now."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _rules(kind="shift_allowance", value=None)

    assert "working_time_night_allowance_type_is_known" in str(raised.value)


def test_an_empty_type_is_refused_too():
    """The shape a row gets when its author never thought about the column."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _rules(kind="", value=None)

    assert "working_time_night_allowance_type_is_known" in str(raised.value)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("percentage", "10.0000"),
        ("fixed_amount", "25.00"),
        ("time_off", None),
        ("by_agreement", None),
    ],
)
def test_every_coherent_pairing_is_accepted(kind, value):
    assert _rules(kind=kind, value=value).pk


def test_a_genuine_zero_is_now_representable_and_is_not_the_same_as_no_figure():
    """The other half of the sentinel's damage, and the half that is easy to
    miss: an instrument that states the allowance IS nil could not be recorded
    at all, because zero already meant something else."""
    nil = _rules(kind="percentage", value="0")
    none_stated = _rules(sector=Sector.objects.create(code=Sector.Code.DOMESTIC, name="D"))

    assert nil.night_allowance_value == Decimal("0")
    assert none_stated.night_allowance_value is None


# ------------------------------------------------------------ and it resolves


def test_the_resolver_answers_with_a_state_rather_than_a_number():
    sector = Sector.objects.create(code=Sector.Code.CONTRACT_CLEANING, name="CC")
    _rules(sector=sector, kind="percentage", value="10.0000", source="SD1 clause 12")

    answer = resolve.night_allowance(sector, datetime.date(2026, 3, 31))

    assert answer.state is NightAllowanceState.PERCENTAGE
    assert answer.value == Decimal("10.0000")
    assert answer.is_payable_by_this_instrument
    assert answer.window_start == datetime.time(18, 0)
    assert answer.source_reference == "SD1 clause 12"


def test_an_instrument_stating_no_figure_resolves_to_by_agreement_and_not_to_zero():
    _rules(source="BCEA 75 of 1997 s17(2)(a)")

    answer = resolve.night_allowance(None, datetime.date(2026, 3, 31))

    assert answer.state is NightAllowanceState.BY_AGREEMENT
    assert answer.value is None
    assert not answer.is_payable_by_this_instrument


def test_no_rule_set_in_force_is_a_fourth_answer_and_not_an_exception():
    """Same shape as ``accommodation_cap()``: the caller must handle "nothing is
    loaded" as an answer, rather than catching it in passing."""
    answer = resolve.night_allowance(None, datetime.date(2026, 3, 31))

    assert answer.state is NightAllowanceState.NOT_LOADED
    assert answer.value is None
    assert answer.detail, "NOT_LOADED must say WHY, in the resolver's own words"


def test_time_off_is_payable_by_nobody_and_owes_nothing():
    """s17(2)(a)'s other limb — the obligation is discharged by reducing hours."""
    _rules(kind="time_off")

    answer = resolve.night_allowance(None, datetime.date(2026, 3, 31))

    assert answer.state is NightAllowanceState.TIME_OFF
    assert not answer.is_payable_by_this_instrument


def test_the_four_type_values_are_the_four_the_calculator_knows():
    from calculators.gross import NightAllowanceKind

    assert {kind.value for kind in NightAllowanceType} == {
        kind.value for kind in NightAllowanceKind
    }


def test_the_resolver_reaches_an_area_scoped_rule_set_and_not_only_a_sector_one():
    """D-266. ONE SECTOR, TWO INSTRUMENTS (D-240): Sectoral Determination 1
    governs contract cleaning in Areas A and C, and the BCCCI Main Agreement
    governs Area B. ``night_allowance()`` took no ``sector_area`` at all, so for
    a KwaZulu-Natal cleaner it answered SD1's row.

    Nothing was ever wrong, because both instruments say 10% of the hourly wage
    in both editions of the agreement. That is the shape of defect this codebase
    keeps finding: right by coincidence, silent until the day the two figures
    diverge — which for a bargaining council is the next time it renegotiates.
    The figures here are deliberately DIFFERENT so the two answers can be told
    apart at all.
    """
    from statutory.models import SectorArea

    sector = Sector.objects.create(code=Sector.Code.CONTRACT_CLEANING, name="CC")
    area_b = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    _rules(sector=sector, kind="percentage", value="10.0000", source="SD1 clause 12")
    _rules(
        sector=sector,
        sector_area=area_b,
        kind="percentage",
        value="12.5000",
        source="BCCCI clause 4.3",
    )

    wide = resolve.night_allowance(sector, datetime.date(2026, 3, 31))
    scoped = resolve.night_allowance(sector, datetime.date(2026, 3, 31), sector_area=area_b)

    assert wide.value == Decimal("10.0000"), "Areas A and C still read SD1"
    assert scoped.value == Decimal("12.5000"), (
        "Area B must read the agreement that binds it, not the determination it points away from"
    )
    assert scoped.source_reference == "BCCCI clause 4.3"
