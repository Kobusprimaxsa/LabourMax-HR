"""s2(1) ADDS the Monday. Both days, or the calendar is wrong (D-280).

Public Holidays Act 36 of 1994 s2(1): "The days mentioned in Schedule 1 shall be
public holidays, and whenever any public holiday falls on a Sunday, the following
Monday shall be a public holiday."

``tools/build_reference_fixture.py`` read that as a MOVE. It emitted one row,
dated the Monday, with ``shifted_from_date`` pointing back at a Sunday that was
never loaded — so for three dates in the shipped corpus
``resolve.is_public_holiday()`` answered False on a day the Act makes a public
holiday. An employee who worked 9 August 2026 was paid the BCEA s16 Sunday rate
and not the s18 public holiday rate; a salaried employee who did not work it lost
the s18(2)(a) paid day. Contract cleaning works Sundays routinely.

**Nothing caught it because there was nothing to catch it with.** This file is
the guard and its refusals, watched failing on every shape they exist for before
being watched staying quiet on a correct pair — a check that has only ever been
run over correct data is indistinguishable from one that returns an empty list.
"""

from __future__ import annotations

import datetime

import pytest

from statutory import checks
from statutory.models import PublicHoliday

CITATION = "Public Holidays Act 36 of 1994, Schedule 1"

#: 9 August 2026 is a Sunday. The date this defect is named by.
WOMENS_DAY_2026 = datetime.date(2026, 8, 9)
MONDAY_AFTER = datetime.date(2026, 8, 10)


def holiday(on, *, name="National Women's Day", shifted_from=None):
    return PublicHoliday.objects.create(
        holiday_date=on,
        name=name,
        shifted_from_date=shifted_from,
        source_reference=CITATION,
    )


def blocking():
    return [issue for issue in checks.check_public_holiday_sundays() if issue.blocking]


# ------------------------------------------------- watching the guard REFUSE


@pytest.mark.statutory
def test_a_sunday_holiday_with_no_monday_is_refused(db):
    """The other direction of the same rule: s2(1) is not optional, so a Sunday
    in the calendar always brings a Monday with it."""
    holiday(WOMENS_DAY_2026)

    issues = blocking()

    assert len(issues) == 1
    assert "2026-08-10 is not loaded" in issues[0].message
    assert "does not move the holiday off the Sunday" in issues[0].message


@pytest.mark.statutory
def test_the_monday_without_its_sunday_is_refused(db):
    """THE SHIPPED DEFECT, reconstructed exactly: one row, dated the Monday,
    pointing back at a Sunday nothing loaded."""
    holiday(MONDAY_AFTER, shifted_from=WOMENS_DAY_2026)

    issues = blocking()

    assert len(issues) == 1
    assert "2026-08-09" in issues[0].message
    assert "is not loaded" in issues[0].message
    assert "adds a day; it does not take one away" in issues[0].message


@pytest.mark.statutory
def test_a_shifted_from_date_that_is_not_a_sunday_is_refused(db):
    """s2(1) only ever adds a Monday to a Sunday. Anything else is a row
    somebody typed a date into."""
    saturday = datetime.date(2026, 12, 26)
    holiday(saturday, name="Day of Goodwill")
    holiday(datetime.date(2026, 12, 28), name="Day of Goodwill", shifted_from=saturday)

    issues = blocking()

    assert len(issues) == 1
    assert "which is a Saturday" in issues[0].message


@pytest.mark.statutory
def test_a_monday_that_is_not_the_day_after_is_refused(db):
    """ "The FOLLOWING Monday" — not the next convenient one. A holiday pushed
    two days out is a day of pay landing on the wrong date."""
    holiday(WOMENS_DAY_2026)
    holiday(MONDAY_AFTER)
    holiday(datetime.date(2026, 8, 17), shifted_from=WOMENS_DAY_2026)

    issues = blocking()

    assert len(issues) == 1
    assert "is not the day after 2026-08-09" in issues[0].message


@pytest.mark.statutory
def test_every_unpaired_sunday_is_named_rather_than_only_the_first(db):
    """Three pairs were broken in the shipped corpus. A check that stops at the
    first one sends somebody back three times."""
    holiday(WOMENS_DAY_2026)
    holiday(datetime.date(2027, 3, 21), name="Human Rights Day")
    holiday(datetime.date(2027, 12, 26), name="Day of Goodwill")

    issues = blocking()

    assert len(issues) == 3
    assert {"2026-08-10", "2027-03-22", "2027-12-27"} == {
        issue.message.split(" is not loaded")[0].split("and ")[-1] for issue in issues
    }


# --------------------------------------------- watching it NOT fire


@pytest.mark.statutory
def test_a_correctly_paired_sunday_and_monday_passes(db):
    """Or every refusal above proves only that a list can be non-empty."""
    holiday(WOMENS_DAY_2026)
    holiday(MONDAY_AFTER, shifted_from=WOMENS_DAY_2026)

    assert checks.check_public_holiday_sundays() == []


@pytest.mark.statutory
def test_an_ordinary_weekday_holiday_is_left_alone(db):
    """Most of the calendar. Freedom Day 2026 is a Monday in its own right and
    carries no shifted_from_date — it must not be read as an added day."""
    holiday(datetime.date(2026, 4, 27), name="Freedom Day")
    holiday(datetime.date(2026, 4, 6), name="Family Day")

    assert checks.check_public_holiday_sundays() == []


@pytest.mark.statutory
def test_an_empty_calendar_is_not_a_finding(db):
    """``run_all`` gates verification of ONE version, and the calendar may not
    be the version being verified. Emptiness is check_something_is_loaded's
    concern (D-74)."""
    assert PublicHoliday.objects.count() == 0
    assert checks.check_public_holiday_sundays() == []


@pytest.mark.statutory
def test_the_check_runs_as_part_of_run_all(db):
    """A guard nothing calls is the failure this codebase keeps shipping."""
    holiday(WOMENS_DAY_2026)

    messages = [issue.message for issue in checks.run_all()]

    assert any("does not move the holiday off the Sunday" in message for message in messages)


# --------------------------------------------- the generator that produced it


@pytest.mark.statutory
@pytest.mark.parametrize(
    ("year", "sunday", "monday"),
    [
        (2026, "2026-08-09", "2026-08-10"),
        (2027, "2027-03-21", "2027-03-22"),
        (2027, "2027-12-26", "2027-12-27"),
    ],
)
def test_the_generator_emits_both_days_for_a_sunday_holiday(year, sunday, monday):
    """The fix at its source. ``public_holidays()`` used to emit ONE row for a
    Sunday holiday, dated the Monday — the fixture is downstream of this."""
    import pathlib
    import sys

    tools = str(pathlib.Path(__file__).resolve().parents[2] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from build_reference_fixture import public_holidays

    rows = {row["holiday_date"]: row for row in public_holidays(year)}

    assert sunday in rows, "Schedule 1 fixes the date; s2(1) does not take it away."
    assert monday in rows, "s2(1) adds the following Monday."
    assert rows[sunday].get("shifted_from_date") is None
    assert rows[monday]["shifted_from_date"] == sunday
    assert rows[sunday]["name"] == rows[monday]["name"]


@pytest.mark.statutory
def test_the_generator_leaves_a_weekday_holiday_as_one_row():
    """Watched NOT firing: 2026 has exactly one Sunday holiday, so the year is
    thirteen rows and not twenty-four."""
    import pathlib
    import sys

    tools = str(pathlib.Path(__file__).resolve().parents[2] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from build_reference_fixture import public_holidays

    rows = public_holidays(2026)

    assert len(rows) == 13
    assert [row["holiday_date"] for row in rows if row.get("shifted_from_date")] == ["2026-08-10"]
