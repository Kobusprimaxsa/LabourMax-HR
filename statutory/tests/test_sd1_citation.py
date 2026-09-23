"""SD1's leave row: the citation must contain every figure it carries (D-279).

Row 142 of the verification workbook cited "clauses 18, 19 and 22" — annual,
sick and family responsibility. The row also carries two MATERNITY figures, and
maternity is clause 20. A verifier sent to 18, 19 and 22 would have found two of
the row's figures nowhere in the clauses they were told to open, and both honest
outcomes are bad: tick anyway, or spend an evening deciding the LOAD is wrong
when only the pinpoint was.

**The figures did not move and this file is what proves it stays that way.**
Every value is pinned below, written down at the moment of the citation fix, so
that any later change to a figure under a citation-only pretext fails here by
name. The loader's own supersede guard refuses a changed figure at load time
(``statutory/tests/test_supersede.py``); this is the standing version of the
same rule, because that guard only ever fires on the day somebody reloads.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from statutory.verification import split_citation

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"

pytestmark = pytest.mark.statutory


@pytest.fixture(scope="module")
def sd1():
    return json.loads((REFERENCE / "ref-2026.03.01-sd1.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def leave_row(sd1):
    (row,) = sd1["tables"]["leave_rule_set"]
    return row


#: Every figure the SD1 leave row carries, as at the D-279 citation fix.
#: Deliberately spelled out rather than compared against another row: the point
#: is a record of what the numbers WERE, and a comparison against something that
#: could move with them proves nothing.
FIGURES = {
    "annual_leave_days_per_cycle_5day": "15.000",
    "annual_leave_days_per_cycle_6day": "18.000",
    "annual_accrual_days_per_month_5day": "1.250",
    "annual_accrual_days_per_month_6day": "1.500",
    "annual_accrual_ratio_days_worked": 17,
    "annual_accrual_ratio_hours_worked": 17,
    "annual_leave_cycle_months": 12,
    "annual_leave_forfeit_months": 6,
    "annual_leave_payable_on_termination": True,
    "has_long_service_annual_leave": False,
    "long_service_annual_leave_years": None,
    "long_service_years_inclusive": None,
    "long_service_annual_leave_days_5day": None,
    "long_service_annual_leave_days_6day": None,
    "sick_leave_cycle_months": 36,
    "sick_leave_weeks_equivalent": "6.00",
    "sick_leave_first_six_months_ratio": 26,
    "sick_leave_payable_on_termination": False,
    "family_responsibility_days": 3,
    "family_resp_min_service_months": 4,
    "family_resp_min_days_per_week": 4,
    "parental_leave_total_months": 4,
    "parental_leave_additional_days": 10,
    "parental_leave_shareable": True,
    "maternity_earliest_start_weeks_before_birth": 4,
    "maternity_no_work_weeks_after_birth": 6,
}


def test_the_citation_fix_moved_no_figure(leave_row):
    """THE ASSERTION THE BRIEF ASKED FOR. r4 became r5 to correct a pinpoint;
    if any of these has changed, that was not a citation fix."""
    for name, expected in FIGURES.items():
        assert leave_row[name] == expected, name


def test_no_figure_was_quietly_added_either(leave_row):
    """The other direction, which the pin above cannot catch on its own: a new
    column appearing in this row is a new figure under an unchanged citation,
    and it would arrive unverified and unnoticed."""
    figures = set(leave_row) - {"sector", "sector_area", "effective_from"}
    figures -= {"source_reference", "source_url", "notes"}

    assert figures == set(FIGURES), "a column appeared or vanished in the SD1 leave row"


# ------------------------------------------- what the pinpoint must contain


def test_the_pinpoint_names_a_clause_for_every_figure_in_the_row(leave_row):
    """Annual is 18, sick 19, MATERNITY 20, family responsibility 22 — the same
    numbering in the determination as published in 1999 and in the consolidation
    as at 1 March 2026 (D-194, D-279). Clause 20 is the one that was missing."""
    _, clause = split_citation(leave_row["source_reference"])

    for number, subject in (
        ("18", "annual"),
        ("19", "sick"),
        ("20", "maternity"),
        ("22", "family"),
    ):
        assert f"{number} ({subject}" in clause, f"clause {number} ({subject}) is not pinpointed"


def test_the_pinpoint_sends_parental_leave_away_from_sd1(leave_row):
    """SD1 states no parental leave at all. The three parental_leave_* columns
    carry the Van Wyk interim reading-in of BCEA s25, so there is no clause to
    cite and pretending otherwise would be the defect this file exists for,
    pointing the other way. O-39 asks whether the columns should be there."""
    _, clause = split_citation(leave_row["source_reference"])

    assert "parental: BCEA s25" in clause


def test_the_document_half_is_unchanged_so_sd1_stays_one_source_document(sd1):
    """D-257: one instrument, one citation string. The workbook groups by the
    document half, so moving a single character of it would split SD1 into two
    source documents — and a version can then be fully checked while still
    reading as incomplete. Every SD1 row must agree here, not just the one that
    changed."""
    documents = {
        split_citation(row["source_reference"])[0]
        for rows in sd1["tables"].values()
        for row in rows
    }

    assert documents == {
        "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text "
        "(clauses 3, 8-24)"
    }


def test_the_citation_fits_the_column(leave_row):
    """``source_reference`` is 200 characters. A pinpoint that names four
    clauses and their subjects comes close enough that this is worth watching:
    silently truncating one would lose the clause it was added for."""
    assert len(leave_row["source_reference"]) <= 200


def test_decimal_figures_are_strings_so_no_float_reaches_the_loader(leave_row):
    """Invariant 6, at the fixture. A JSON number for a leave day count would
    arrive as a float."""
    for name in (
        "annual_leave_days_per_cycle_5day",
        "annual_accrual_days_per_month_5day",
        "sick_leave_weeks_equivalent",
    ):
        assert isinstance(leave_row[name], str), name
        Decimal(leave_row[name])
