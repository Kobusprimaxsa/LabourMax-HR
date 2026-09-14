"""termination_notice_band — D-68, closed.

Two things this file exists to prove, each by watching the guard fail first:

- the shipped fixture's BCEA and domestic bands are the SAME figures the
  retired ``notice_weeks_*`` columns held (task 2's comparison), so the only
  real change in this closure is SD1's two new bands
- ``resolve.notice_band()`` picks the right band, including on the exact
  boundary, and ``checks.check_notice_bands()`` refuses a rule set whose
  bands do not start at zero, touch exactly, or have exactly one open end
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from statutory import checks, resolve
from statutory.models import Sector, TerminationNoticeBand, TerminationRuleSet

CITATION = "Test fixture, not a real determination"


def rule_set(*, sector=None) -> TerminationRuleSet:
    return TerminationRuleSet.objects.create(
        sector=sector,
        effective_from=datetime.date(2026, 3, 1),
        source_reference=CITATION,
        severance_weeks_per_completed_year=Decimal("1.00"),
        severance_requires_operational_reason=True,
        annual_bonus_weeks=Decimal("0.000"),
        annual_bonus_month=None,
        annual_bonus_pro_rata_on_termination=False,
        annual_bonus_min_service_months=0,
    )


def add_band(
    rule, *, sequence, from_value, from_unit, to_value, to_unit, notice_value, notice_unit
):
    return TerminationNoticeBand.objects.create(
        termination_rule_set=rule,
        sequence=sequence,
        source_reference=CITATION,
        service_from_value=Decimal(from_value),
        service_from_unit=from_unit,
        service_to_value=None if to_value is None else Decimal(to_value),
        service_to_unit="" if to_value is None else to_unit,
        notice_value=Decimal(notice_value),
        notice_unit=notice_unit,
    )


# ------------------------------------------------------------- task 2: the carry-over


@pytest.mark.statutory
def test_the_shipped_bcea_and_domestic_bands_reproduce_the_retired_columns():
    """The evidence nothing moved except SD1.

    ``termination_rule_set`` used to carry these as
    ``notice_weeks_under_6_months`` / ``notice_weeks_6_months_and_over`` /
    ``notice_weeks_over_1_year``. Those three columns are gone (dropped in
    the same migration that added this table), so this asserts against a
    frozen, independently-transcribed snapshot of what they said rather than
    against the columns themselves — the only durable way to keep proving
    the carry-over once the thing being carried FROM no longer exists.

    BCEA: 1 / 2 / 4 weeks (ss 37 and 41). Domestic: 1 / 4 / 4 weeks (SD7,
    which collapses to two distinct bands since the last two agree).
    """
    import json
    import pathlib

    fixture_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "reference"
        / "ref-2026.03.01-notice-bands.json"
    )
    document = json.loads(fixture_path.read_text(encoding="utf-8"))
    rows = document["tables"]["termination_notice_band"]

    def value_at(sector, sequence):
        (row,) = [r for r in rows if r.get("sector") == sector and r["sequence"] == sequence]
        return Decimal(row["notice_value"]), row["notice_unit"]

    # BCEA default (sector null): the retired notice_weeks_under_6_months=1.00,
    # notice_weeks_6_months_and_over=2.00, notice_weeks_over_1_year=4.00.
    assert value_at(None, 1) == (Decimal("1"), "weeks")
    assert value_at(None, 2) == (Decimal("2"), "weeks")
    assert value_at(None, 3) == (Decimal("4"), "weeks")

    # Domestic: the retired 1.00 / 4.00 / 4.00 — two bands, since 4 == 4.
    assert value_at("DOMESTIC", 1) == (Decimal("1"), "weeks")
    assert value_at("DOMESTIC", 2) == (Decimal("4"), "weeks")
    assert not [r for r in rows if r.get("sector") == "DOMESTIC" and r["sequence"] == 3]


# ---------------------------------------------------------------- resolve.notice_band


@pytest.mark.statutory
def test_a_service_length_exactly_on_a_boundary_resolves_to_the_upper_band(db):
    """D-158. Among every band whose start has been reached, the one that
    started MOST RECENTLY wins — the same inclusive-start/exclusive-end
    convention every effective-dated range in this schema already uses.
    """
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="0",
        from_unit="weeks",
        to_value="4",
        to_unit="weeks",
        notice_value="1",
        notice_unit="days",
    )
    add_band(
        rule,
        sequence=2,
        from_value="4",
        from_unit="weeks",
        to_value=None,
        to_unit="",
        notice_value="4",
        notice_unit="weeks",
    )
    started = datetime.date(2026, 3, 1)
    on_exactly_4_weeks = started + datetime.timedelta(weeks=4)

    band = resolve.notice_band(None, on_exactly_4_weeks, employment_start_date=started)

    assert band.sequence == 2
    assert band.notice_value == 4
    assert band.notice_unit == "weeks"

    # One day short of the boundary is still band 1 — proves the test isn't
    # accidentally passing because band 2 matches everything.
    band = resolve.notice_band(
        None, on_exactly_4_weeks - datetime.timedelta(days=1), employment_start_date=started
    )
    assert band.sequence == 1


@pytest.mark.statutory
def test_a_rule_set_with_no_bands_loaded_raises(db):
    """The rule set itself resolves fine — it is this table specifically
    that has nothing in it, which termination_rules() alone cannot see.
    """
    cleaning = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    rule_set(sector=cleaning)  # no add_band() calls at all

    with pytest.raises(resolve.StatutoryValueMissingError) as raised:
        resolve.notice_band(
            cleaning, datetime.date(2026, 6, 1), employment_start_date=datetime.date(2026, 3, 1)
        )

    assert "notice band" in str(raised.value).lower()
    assert "contract_cleaning" in str(raised.value).lower()


@pytest.mark.statutory
def test_no_rule_set_at_all_raises_before_notice_band_gets_a_say(db):
    """No termination_rule_set for this sector or the BCEA default at all —
    termination_rules() itself raises first, the same way every other rule
    set lookup does.
    """
    cleaning = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )

    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.notice_band(
            cleaning, datetime.date(2026, 6, 1), employment_start_date=datetime.date(2026, 3, 1)
        )


# --------------------------------------------------------------- checkstatutory


@pytest.mark.statutory
def test_a_consistent_band_set_reports_nothing(db):
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="0",
        from_unit="months",
        to_value="6",
        to_unit="months",
        notice_value="1",
        notice_unit="weeks",
    )
    add_band(
        rule,
        sequence=2,
        from_value="6",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="4",
        notice_unit="weeks",
    )

    assert checks.check_notice_bands() == []


@pytest.mark.statutory
def test_a_gap_between_bands_is_refused(db):
    """Band 2 starts at 7 months, not 6 — a service length of exactly 6
    months and three weeks would resolve to no band at all.
    """
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="0",
        from_unit="months",
        to_value="6",
        to_unit="months",
        notice_value="1",
        notice_unit="weeks",
    )
    add_band(
        rule,
        sequence=2,
        from_value="7",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="4",
        notice_unit="weeks",
    )

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "touch exactly" in str(issues[0])


@pytest.mark.statutory
def test_an_overlap_between_bands_is_refused(db):
    """Band 2 starts at 5 months while band 1 runs to 6 — both claim month 5."""
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="0",
        from_unit="months",
        to_value="6",
        to_unit="months",
        notice_value="1",
        notice_unit="weeks",
    )
    add_band(
        rule,
        sequence=2,
        from_value="5",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="4",
        notice_unit="weeks",
    )

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "touch exactly" in str(issues[0])


@pytest.mark.statutory
def test_a_set_with_no_open_ended_band_is_refused(db):
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="0",
        from_unit="months",
        to_value="6",
        to_unit="months",
        notice_value="1",
        notice_unit="weeks",
    )
    add_band(
        rule,
        sequence=2,
        from_value="6",
        from_unit="months",
        to_value="12",
        to_unit="months",
        notice_value="4",
        notice_unit="weeks",
    )

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "0 open-ended" in str(issues[0]) or "open-ended" in str(issues[0])


@pytest.mark.statutory
def test_a_set_with_two_open_ended_bands_is_refused(db):
    """Unbounded at both ends: band 1 is also open-ended, which is nonsense
    for the shortest-service band and would make the reconciliation
    ambiguous about which one is really the top."""
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="0",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="1",
        notice_unit="weeks",
    )
    add_band(
        rule,
        sequence=2,
        from_value="6",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="4",
        notice_unit="weeks",
    )

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "2 open-ended" in str(issues[0])


@pytest.mark.statutory
def test_a_band_starting_above_zero_is_refused(db):
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="1",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="1",
        notice_unit="weeks",
    )

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "not 0" in str(issues[0])


@pytest.mark.statutory
def test_a_single_band_unbounded_at_both_ends_is_refused(db):
    """One band, starting above zero AND open-ended: it covers everything
    from six months onward while leaving everything below six months
    covered by nothing at all — unbounded in the sense that matters (no
    edge is actually anchored), not merely "has an open top", which on its
    own is correct for a genuine top band.
    """
    rule = rule_set()
    add_band(
        rule,
        sequence=1,
        from_value="6",
        from_unit="months",
        to_value=None,
        to_unit="",
        notice_value="4",
        notice_unit="weeks",
    )

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "not 0" in str(issues[0])


@pytest.mark.statutory
def test_a_rule_set_with_no_bands_at_all_is_refused(db):
    rule_set()

    issues = checks.check_notice_bands()

    assert issues and all(issue.blocking for issue in issues)
    assert "no notice bands" in str(issues[0])
