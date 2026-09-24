"""The grid's cell grammar, without a database (D-299)."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.cellcodes import CellCodeError, CellEntry, parse, shown
from attendance.models import AttendanceDay

TUESDAY = datetime.date(2026, 6, 2)
SUNDAY = datetime.date(2026, 6, 7)


def p(text, day=TUESDAY, holiday=False):
    return parse(text, work_date=day, is_public_holiday=holiday)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("9", CellEntry("ordinary", Decimal("9"))),
        ("8.5", CellEntry("ordinary", Decimal("8.5"))),
        ("8,5", CellEntry("ordinary", Decimal("8.5"))),
        (" o ", CellEntry("ordinary", use_schedule=True)),
        ("O9", CellEntry("ordinary", Decimal("9"))),
        ("R", CellEntry("rest_day")),
        ("a", CellEntry("absent_unpaid")),
        ("P", CellEntry("absent_paid")),
        ("N", CellEntry("no_work_available")),
        ("H", CellEntry("public_holiday")),
        ("H6", CellEntry("public_holiday", Decimal("6"))),
        ("B8", CellEntry("ordinary", Decimal("8"), is_standby=True)),
    ],
)
def test_what_each_code_means(typed, expected):
    assert p(typed) == expected


def test_bare_hours_take_their_type_from_the_date():
    assert p("4", day=SUNDAY).day_type == "sunday"
    assert p("4", holiday=True).day_type == "public_holiday"
    assert p("4", day=SUNDAY, holiday=True).day_type == "public_holiday", "O-40 goes to H"


@pytest.mark.parametrize(
    ("typed", "said"),
    [
        ("", "Empty"),
        ("   ", "Empty"),
        ("X", "not a day type"),
        ("L", "leave application"),
        ("S", "needs its hours"),
        ("B", "needs the hours"),
        ("A4", "Absent — unpaid takes no hours"),
        ("0", "more than 0"),
        ("24.5", "no more than 24"),
        ("9h", "not a code"),
        ("99", "no more than 24"),
    ],
)
def test_every_refusal_says_why(typed, said):
    with pytest.raises(CellCodeError, match=said):
        p(typed)


def test_what_a_cell_shows_retypes_to_the_same_thing():
    day = AttendanceDay(
        work_date=TUESDAY,
        day_type="ordinary",
        ordinary_hours=Decimal("8.000"),
        overtime_hours=Decimal("1.000"),
        sunday_hours=Decimal("0"),
        public_holiday_hours=Decimal("0"),
        standby_hours_worked=Decimal("0"),
    )
    assert shown(day) == "9"
    assert p(shown(day)) == CellEntry("ordinary", Decimal("9"))

    rest = AttendanceDay(
        work_date=TUESDAY,
        day_type="rest_day",
        ordinary_hours=Decimal("0"),
        overtime_hours=Decimal("0"),
        sunday_hours=Decimal("0"),
        public_holiday_hours=Decimal("0"),
        standby_hours_worked=Decimal("0"),
    )
    assert shown(rest) == "R"
