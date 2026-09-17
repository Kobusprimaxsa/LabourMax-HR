"""The accommodation cap is a PAIR, and the pair is enforced in the database.

D-198 amended. ``accommodation_deduction_max_pct = 0.00`` used to stand in for
"this instrument states no cap": a value standing in for the absence of one. It
made a genuine zero cap unrepresentable — read as UNLIMITED, the maximally wrong
answer — and, worse, it made a new rule set row whose author never thought about
the column silently uncapped. The boolean takes NO database default precisely so
that row fails instead.

Constraints are asserted from ``pg_constraint`` rather than from the model: a
CheckConstraint declared on an abstract base is NOT inherited by a child that
declares its own Meta, and every model here declares one for ``db_table``, so
"it is in the model" is not evidence that it is in the database.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from statutory import resolve
from statutory.models import Sector, WorkingTimeRuleSet
from statutory.resolve import AccommodationCapState

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

TABLE = "working_time_rule_set"
PAIR_CONSTRAINTS = {
    "working_time_capped_states_its_percentage",
    "working_time_uncapped_states_no_percentage",
}


def _rules(*, sector=None, capped, ceiling=None, source="Test fixture"):
    return WorkingTimeRuleSet.objects.create(
        sector=sector,
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
        night_allowance_type="percentage",
        night_allowance_value=Decimal("10"),
        standby_allowance_per_shift=Decimal("50.00"),
        standby_window_start=datetime.time(18, 0),
        standby_window_end=datetime.time(6, 0),
        standby_hours_before_overtime=Decimal("2"),
        min_paid_hours_per_day=Decimal("6"),
        meal_interval_after_hours=Decimal("5"),
        meal_interval_minutes=60,
        daily_rest_hours=12,
        weekly_rest_hours=36,
        accommodation_deduction_capped=capped,
        accommodation_deduction_max_pct=None if ceiling is None else Decimal(ceiling),
    )


def test_both_pair_constraints_exist_in_the_database():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'",
            [TABLE],
        )
        landed = {row[0] for row in cursor.fetchall()}
    assert PAIR_CONSTRAINTS <= landed, f"missing: {sorted(PAIR_CONSTRAINTS - landed)}"


def test_the_boolean_is_not_null_and_has_no_database_default():
    """THE POINT OF THE WHOLE RE-ENCODING. A default would make a row that never
    considered accommodation quietly take that default's meaning."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT is_nullable, column_default FROM information_schema.columns
            WHERE table_name = %s AND column_name = 'accommodation_deduction_capped'
            """,
            [TABLE],
        )
        is_nullable, column_default = cursor.fetchone()
    assert is_nullable == "NO"
    assert column_default is None, f"a default would defeat the pair: {column_default!r}"


def test_a_row_that_does_not_say_is_refused_by_the_database():
    """Written straight to SQL, the way a loader or a data migration would, with
    every other NOT NULL column filled in and only this one left out."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO working_time_rule_set (
                    effective_from, source_reference, notes, source_url,
                    ordinary_hours_per_week, ordinary_hours_per_day_5day,
                    ordinary_hours_per_day_6day, overtime_multiplier,
                    max_overtime_hours_per_day, max_overtime_hours_per_week,
                    sunday_multiplier_ordinary, sunday_multiplier_non_ordinary,
                    public_holiday_worked_multiplier, public_holiday_not_worked_paid,
                    night_work_start_time, night_work_end_time, night_allowance_type,
                    night_allowance_value, standby_allowance_per_shift,
                    standby_window_start, standby_window_end,
                    standby_hours_before_overtime, min_paid_hours_per_day,
                    meal_interval_after_hours, meal_interval_minutes,
                    daily_rest_hours, weekly_rest_hours, created_at, updated_at
                ) VALUES (
                    DATE '1997-12-01', 'Test fixture', '', '',
                    45, 9, 8, 1.5, 3, 10, 1.5, 2.0, 2.0, TRUE,
                    TIME '18:00', TIME '06:00', 'percentage', 10, 50.00,
                    TIME '18:00', TIME '06:00', 2, 6, 5, 60, 12, 36, now(), now()
                )
                """
            )
    assert 'null value in column "accommodation_deduction_capped"' in str(raised.value)


@pytest.mark.parametrize(
    ("capped", "ceiling", "name"),
    [
        (True, None, "capped_states_its_percentage"),
        (False, "10.00", "uncapped_states_no_percentage"),
    ],
)
def test_a_half_written_pair_is_refused(capped, ceiling, name):
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _rules(capped=capped, ceiling=ceiling)
    assert f"working_time_{name}" in str(raised.value)


# ------------------------------------------------------------- the three states


def test_not_loaded_when_no_rule_set_is_in_force(db):
    cap = resolve.accommodation_cap(None, datetime.date(2026, 3, 1))
    assert cap.state is AccommodationCapState.NOT_LOADED
    assert "No working time rule set" in cap.detail
    assert cap.exceeded_by(Decimal("99")) is False, "NOT_LOADED is not a cap; the caller refuses"


def test_no_cap_is_not_a_cap_of_zero():
    _rules(capped=False, source="BCEA: states no accommodation percentage")
    cap = resolve.accommodation_cap(None, datetime.date(2026, 3, 1))
    assert cap.state is AccommodationCapState.NO_CAP
    assert cap.percentage is None
    assert cap.exceeded_by(Decimal("99")) is False


def test_capped_at_zero_is_exceeded_by_anything_above_zero():
    """The case the sentinel could not express at all."""
    _rules(capped=True, ceiling="0.00")
    cap = resolve.accommodation_cap(None, datetime.date(2026, 3, 1))
    assert cap.state is AccommodationCapState.CAPPED
    assert cap.percentage == Decimal("0.00")
    assert cap.exceeded_by(Decimal("0.01")) is True
    assert cap.exceeded_by(Decimal("0")) is False


def test_capped_resolves_the_sector_row_over_the_bcea_default():
    sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    _rules(capped=False, source="BCEA")
    _rules(sector=sector, capped=True, ceiling="10.00", source="SD7")
    cap = resolve.accommodation_cap(sector, datetime.date(2026, 3, 1))
    assert cap.state is AccommodationCapState.CAPPED
    assert cap.percentage == Decimal("10.00")
    assert cap.source_reference == "SD7"
    assert cap.exceeded_by(Decimal("10.01")) is True
    assert cap.exceeded_by(Decimal("10.00")) is False
