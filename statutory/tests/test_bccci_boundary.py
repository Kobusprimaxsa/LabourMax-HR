"""Every Area B figure, on 31 March 2026 and on 1 April 2026 (D-265).

The two BCCCI agreements abut on 1 April 2026, and by that date this build had
loaded them in three separate chunks — wages, then leave and working time, then
termination and notice. Each chunk tested its own rows. Nothing had ever walked
the whole boundary in one pass and said, figure by figure, what an Area B
employee is owed on each side of it.

**Every expected value below is written out as a literal, transcribed from the
gazette and not computed from the other side.** A boundary test that derives one
side from the other cannot fail in the one direction that matters: a rate loaded
against the wrong effective date reads as consistent with itself.

The two documents:

* PREDECESSOR — Notice 1726 of 2023 in GG 48356, 31 March 2023, in force to
  31 March 2026 inclusive.
* SUCCESSOR — GN R.7296 in GG 54412, 27 March 2026, in force from 1 April 2026.

**Almost everything is the same on both sides, and that is the finding** — the
council reissued its agreement with one figure changed and one clause inserted.
The differences are named in D-265; the sameness is asserted here, because a
figure this build ASSUMED was unchanged is a figure nobody checked.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import resolve
from statutory.loader import load_reference_data
from statutory.models import Sector, SectorArea

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"

#: Loaded in FIXTURE_ORDER's own order: the predecessor's rows close on the
#: successor's start date, so the successor must arrive second.
FIXTURES = (
    "ref-2023.04.01-bccci.json",
    "ref-2023.04.01-bccci-rules.json",
    "ref-2023.04.01-bccci-termination.json",
    "ref-2026.04.01-bccci.json",
    "ref-2026.04.01-bccci-rules.json",
    "ref-2026.04.01-bccci-notice.json",
    "ref-2026.04.01-bccci-maternity.json",
)

LAST_DAY = datetime.date(2026, 3, 31)
FIRST_DAY = datetime.date(2026, 4, 1)


@pytest.fixture
def area_b(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING,
        name="Contract cleaning sector",
        determination_reference="Sectoral Determination 1",
    )
    area = SectorArea.objects.create(
        sector=sector,
        code="AREA_B",
        name="Area B",
        uses_bargaining_council_rates=True,
    )
    for name in FIXTURES:
        path = REFERENCE / name
        if path.exists():
            load_reference_data(json.loads(path.read_text(encoding="utf-8")))
    return sector, area


# --------------------------------------------------------------- what CHANGED


def test_the_hourly_wage_is_the_one_figure_that_moves(area_b):
    """cl 4.1(a)(iii) of the predecessor against cl 4.1(a)(i) of the successor.
    R1,54 an hour, and the only figure in either instrument that differs."""
    sector, area = area_b

    before = resolve.minimum_wage(LAST_DAY, sector=sector, sector_area=area)
    after = resolve.minimum_wage(FIRST_DAY, sector=sector, sector_area=area)

    assert before.hourly_rate == Decimal("30.8600")
    assert after.hourly_rate == Decimal("32.4000")


# ------------------------------- what is the SAME, asserted rather than assumed


def test_the_december_bonus_is_4_33_weeks_on_both_sides(area_b):
    """Predecessor cl 4.5(a), successor cl 4.5(a). NOT Sectoral Determination
    1's 4,333 weeks — two figures for one concept that look almost identical,
    which is what D-236 exists to keep apart."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        rules = resolve.termination_rules(sector, on_date, sector_area=area)
        assert rules.annual_bonus_weeks == Decimal("4.330"), on_date
        assert rules.annual_bonus_month == 12, on_date
        assert rules.annual_bonus_pro_rata_on_termination is True, on_date
        assert rules.annual_bonus_min_service_months == 0, on_date


def test_severance_is_one_week_per_completed_year_on_both_sides(area_b):
    """Predecessor cl 35.2, successor cl 36.2 — the BCEA s41 position, restated
    by the council and unchanged between the two."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        rules = resolve.termination_rules(sector, on_date, sector_area=area)
        assert rules.severance_weeks_per_completed_year == Decimal("1.00"), on_date
        assert rules.severance_requires_operational_reason is True, on_date


def test_the_night_allowance_is_ten_percent_on_both_sides(area_b):
    """Predecessor cl 4.3, successor cl 4.3, word for word."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        allowance = resolve.night_allowance(sector, on_date, sector_area=area)
        assert allowance.value == Decimal("10.000"), on_date
        assert allowance.is_payable_by_this_instrument, on_date


def test_annual_leave_is_the_same_on_both_sides_including_the_long_service_band(area_b):
    """Predecessor cl 8.1, successor cl 9.1 — the clause moved by one and the
    figures did not move at all."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        recent = resolve.annual_leave_days(
            sector,
            on_date,
            employment_start_date=on_date - datetime.timedelta(days=400),
            six_day_week=True,
            sector_area=area,
        )
        long_serving = resolve.annual_leave_days(
            sector,
            on_date,
            employment_start_date=on_date - datetime.timedelta(days=365 * 11),
            six_day_week=True,
            sector_area=area,
        )
        # Clause 9.1 states 21 and 28 CONSECUTIVE days; the stored columns are
        # the working days those come to on a six-day week, which is what the
        # accrual engine spends. Both readings are in the row's own note.
        assert recent == Decimal("18.000"), on_date
        assert long_serving == Decimal("24.000"), on_date


def test_the_notice_contradiction_refuses_identically_on_both_sides(area_b):
    """Predecessor cl 20.1(b), successor cl 21.1(b). THE DEFECT IS INHERITED
    (D-261): an employee between four weeks and six months gets no answer from
    either agreement, and notice is symmetric so there is no safe direction to
    guess in."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        with pytest.raises(resolve.StatutoryValueMissingError) as caught:
            resolve.notice_band(
                sector,
                on_date,
                employment_start_date=on_date - datetime.timedelta(days=60),
                sector_area=area,
            )
        assert "two irreconcilable answers" in str(caught.value), on_date
        assert "probation" in str(caught.value).lower(), on_date


@pytest.mark.parametrize(
    ("service_days", "expected_value", "expected_unit"),
    [(10, Decimal("1"), "days"), (400, Decimal("2"), "weeks")],
)
def test_the_two_uncontested_notice_bands_are_the_same_on_both_sides(
    area_b, service_days, expected_value, expected_unit
):
    """One working day during the first four weeks, two weeks from six months
    on — a day, not a fifth of a week, because contract cleaning runs six-day
    weeks (D-68)."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        band = resolve.notice_band(
            sector,
            on_date,
            employment_start_date=on_date - datetime.timedelta(days=service_days),
            sector_area=area,
        )
        assert band.notice_value == expected_value, on_date
        assert band.notice_unit == expected_unit, on_date


def test_the_four_point_three_three_derivation_factor_holds_on_both_sides(area_b):
    """Predecessor cl 3, successor cl 3 — 4,33 rather than the BCEA's four and
    one third, scoped to this sector and area (D-236)."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        factor = resolve.parameter_value(
            "MONTHLY_TO_WEEKLY_FACTOR", on_date, sector=sector, sector_area=area
        )
        assert factor == Decimal("4.330000"), on_date


def test_the_six_hour_daily_minimum_holds_on_both_sides(area_b):
    """The one place the two instruments are numbered differently for the SAME
    rule: predecessor cl 4.6(a), successor cl 5. Reading the successor's
    numbering back onto the predecessor lands on Payment of Remuneration."""
    sector, area = area_b

    for on_date in (LAST_DAY, FIRST_DAY):
        rules = resolve.working_time_rules(sector, on_date, sector_area=area)
        assert rules.min_paid_hours_per_day == Decimal("6.00"), on_date


# --------------------------------------------------- the boundary is exclusive


def test_nothing_resolves_to_the_wrong_side_by_one_day(area_b):
    """The half-open convention, exercised where it actually costs money:
    31 March pays R30,86 and 1 April pays R32,40, with no day answering both
    and no day answering neither."""
    sector, area = area_b

    def wage(on_date):
        return resolve.minimum_wage(on_date, sector=sector, sector_area=area).hourly_rate

    assert wage(datetime.date(2026, 3, 30)) == Decimal("30.8600")
    assert wage(LAST_DAY) == Decimal("30.8600")
    assert wage(FIRST_DAY) == Decimal("32.4000")
    assert wage(datetime.date(2026, 4, 2)) == Decimal("32.4000")
