"""BCCCI clause 21.1(b)'s probation exception, at every boundary (D-277).

D-241 read the clause's two limbs as a contradiction and loaded the window
CONTESTED, so notice between four weeks and six months refused. It is not a
contradiction. The two-week limb is general — "after the first four weeks of
such employment", qualified by nothing. The one-week limb is addressed "to an
employee whilst on probation, as defined". A general rule and an exception
stated for a named class govern different people; reading them as rivals is
what made the window look irresolvable.

**Notice is symmetric** (SD1 cl 23(1)(c), D-158), so the band cannot depend on
who is giving notice — and the table has no column for that, which is the
design saying the same thing. Every test here therefore resolves the band once
and asserts it is the answer for a resignation and a dismissal alike; the two
are the same lookup, and a test that passed a reason code would be testing an
argument that does not exist.

The dates are counted rather than named: four weeks from 1 April 2026 is
29 April, four months is 1 August, six months is 1 October.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import checks, resolve
from statutory.loader import load_reference_data
from statutory.models import (
    Sector,
    SectorArea,
    TerminationNoticeBand,
    TerminationRuleSet,
)

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"

STARTED = datetime.date(2026, 4, 1)
ONE_DAY = Decimal("1.00")
ONE_WEEK = Decimal("1.00")
TWO_WEEKS = Decimal("2.00")


@pytest.fixture
def area_b(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    area = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    for name in (
        "ref-2026.03.01-leave.json",
        "ref-2026.03.01-rules.json",
        "ref-2026.03.01-sd1.json",
        "ref-2026.03.01-notice-bands.json",
        "ref-2026.04.01-bccci-rules.json",
        "ref-2026.04.01-bccci-notice.json",
    ):
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))
    return sector, area


def band_on(area_b, on_date, *, on_probation=None):
    sector, area = area_b
    return resolve.notice_band(
        sector,
        on_date,
        employment_start_date=STARTED,
        sector_area=area,
        on_probation=on_probation,
    )


# --------------------------------------------------------------- the boundaries


def test_exactly_four_weeks_is_still_the_first_four_weeks(area_b):
    """ "DURING the first four weeks" claims the boundary for the lower band
    (D-158), and that band is unconditional — clause 21.1(b)(i) says nothing
    about probation, so the answer is one working day for everybody and the
    lane never comes into it."""
    assert band_on(area_b, datetime.date(2026, 4, 29)).notice_value == ONE_DAY


def test_a_day_past_four_weeks_on_probation_is_one_week(area_b):
    assert band_on(area_b, datetime.date(2026, 4, 30), on_probation=True).notice_value == ONE_WEEK


def test_a_day_past_four_weeks_off_probation_is_two_weeks(area_b):
    """THE WHOLE POINT. The same day, the same employee's service length, and a
    different answer — which is why the condition had to be data rather than a
    reading somebody takes at the time."""
    band = band_on(area_b, datetime.date(2026, 4, 30), on_probation=False)
    assert band.notice_value == TWO_WEEKS
    assert band.notice_unit == "weeks"


def test_exactly_four_months_with_probation_just_ended_is_two_weeks(area_b):
    """Clause 3 caps probation at four months, so this is the last day the
    one-week answer can exist at all — and an employee whose probation has run
    out is, from that moment, an ordinary employee on the general rule."""
    assert band_on(area_b, datetime.date(2026, 8, 1), on_probation=False).notice_value == TWO_WEEKS


def test_exactly_six_months_is_still_the_middle_window(area_b):
    """ "between 4 weeks ... and six months", and the band claims its upper
    boundary inclusively. Off probation the figure is the same two weeks the
    band above gives, which is not an accident — it is the general rule running
    through both."""
    assert band_on(area_b, datetime.date(2026, 10, 1), on_probation=False).notice_value == TWO_WEEKS


def test_a_day_past_six_months_is_still_the_off_probation_lane(area_b):
    """CHANGED 25 Sep 2026 (D-315, replacing the test that the band here was
    unconditional). Since the three-row restructure the off-probation lane runs
    from four weeks with no end, so a day past six months reads it: still two
    weeks, now from the same row as a day past four weeks."""
    band = band_on(area_b, datetime.date(2026, 10, 2), on_probation=False)
    assert band.notice_value == TWO_WEEKS
    assert band.probation_condition == "off_probation"
    assert band.service_to_value is None


# ------------------------------------------------- symmetry, and what it means


@pytest.mark.parametrize("on_probation", [True, False])
def test_resignation_and_dismissal_get_the_same_band(area_b, on_probation):
    """Notice is SYMMETRIC — SD1 cl 23(1)(c) forbids an employee owing more
    notice than the employer does — so who is giving notice cannot move the
    figure. Proven by the lookup taking no reason code and the band being one
    row: a resigning employee and a dismissed one read the same cell, on the
    same day, with the same probation status."""
    day = datetime.date(2026, 5, 15)
    resigning = band_on(area_b, day, on_probation=on_probation)
    dismissed = band_on(area_b, day, on_probation=on_probation)

    assert resigning.pk == dismissed.pk
    assert resigning.notice_value == (ONE_WEEK if on_probation else TWO_WEEKS)


# ------------------------------------------- what happens when nobody says


def test_not_saying_whether_the_employee_is_on_probation_refuses(area_b):
    """THE GUARD, watched failing. Defaulting to the unconditional reading
    would be quietly wrong here, because there is no unconditional band in this
    window at all — the caller would get whichever lane the query returned
    first, which is one week or two for a real person by accident of row
    order."""
    with pytest.raises(resolve.StatutoryValueMissingError) as raised:
        band_on(area_b, datetime.date(2026, 7, 1))

    message = str(raised.value)
    assert "on probation" in message
    assert "did not say" in message
    assert "GN R.7296" in message, "name the instrument, so the reader can go and look"
    assert "on probation: 1.00 weeks" in message, "state the answer for each lane"
    assert "probation has ended: 2.00 weeks" in message


def test_the_refusal_does_not_fire_where_the_instrument_never_asks(area_b):
    """Watched NOT firing. Every other instrument in the corpus is silent about
    probation, and a caller that has never heard of it must go on working —
    which is most of the callers, and all of the ones written before D-277."""
    sector, _ = area_b
    band = resolve.notice_band(sector, datetime.date(2026, 7, 1), employment_start_date=STARTED)

    assert band.notice_value == Decimal("4.00"), "SD1's own answer, sector-wide"


# ---------------------------------------------------- the predecessor agreement


def test_the_2023_agreement_resolves_the_same_way(db):
    """Clause 20.1(b) is clause 21.1(b) word for word (D-261), so resolving one
    and leaving the other contested would have answered an April 2026
    termination and refused a March 2026 one on identical wording."""
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    area = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    for name in (
        "ref-2026.03.01-leave.json",
        "ref-2026.03.01-rules.json",
        "ref-2026.03.01-sd1.json",
        "ref-2026.03.01-notice-bands.json",
        "ref-2023.04.01-bccci-rules.json",
        "ref-2023.04.01-bccci-termination.json",
    ):
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))

    started = datetime.date(2025, 12, 1)
    march = datetime.date(2026, 3, 15)

    on = resolve.notice_band(
        sector, march, employment_start_date=started, sector_area=area, on_probation=True
    )
    off = resolve.notice_band(
        sector, march, employment_start_date=started, sector_area=area, on_probation=False
    )

    assert on.notice_value == ONE_WEEK
    assert off.notice_value == TWO_WEEKS
    assert "Notice 1726 of 2023" in on.source_reference


# ------------------------------------------- the reconciliation, watched failing


def test_a_probation_band_with_no_twin_reads_as_a_gap(area_b):
    """PROVE EVERY GUARD FAILS. Stating "one week while on probation" and
    forgetting the other lane leaves every employee who is NOT on probation
    with no band at all between four weeks and six months — a hole that the old
    check could not see, because in sequence order the bands still touched.

    Watched NOT firing first: the loaded data passes.
    """
    assert not checks.check_notice_bands(), "the shipped bands reconcile in both lanes"

    twin = TerminationNoticeBand.objects.get(probation_condition="off_probation")
    twin.delete()

    issues = checks.check_notice_bands()

    assert issues, "a lane with a hole in it must be reported"
    message = " ".join(str(issue) for issue in issues)
    assert "off probation" in message, "name the lane, or nobody knows which half is missing"


def test_an_instrument_that_never_mentions_probation_is_one_lane(area_b):
    """Watched NOT firing, the other way round. The BCEA default appears in
    every run of checkstatutory, and splitting it into halves it does not have
    would put a probation clause into a message that has no business carrying
    one — and would double every issue it ever reports."""
    bcea = TerminationRuleSet.objects.get(sector__isnull=True)

    lanes = checks._probation_lanes(list(bcea.notice_bands.all()))

    assert len(lanes) == 1
    assert lanes[0][0] == "", "no lane label, so no lane clause in the message"
