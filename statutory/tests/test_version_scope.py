"""When a reference version stops applying, and who needs to know (D-278).

``applies_from`` alone made every version that ever applied applicable forever.
The 2023 BCCCI agreement applies from 1 April 2023 and every row it loaded
closes on 1 April 2026, so a June 2026 payroll run can read nothing from it —
and the gate was still naming four versions of it among the things somebody
must go and verify before anyone gets paid.

That is not a small untidiness. A refusal that lists work nobody can do is one
people learn to read past, which is exactly the failure D-271 fixed in
``checkstatutory`` a month earlier. And the verification workbook was doing the
same thing from the other end, reporting two superseded versions as
"0 of 0 checked" — a line that reads like a broken report and sent somebody
looking for figures that were never there.

``applies_until`` is DERIVED at load and never declared. An author asked to
state it would be stating twice what the rows already say, and the second
statement is the one that goes stale.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from statutory.loader import load_reference_data, scope_of
from statutory.models import ReferenceDataVersion

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]


def document(label, *, rows):
    return {
        "version_label": label,
        "applies_from": "2023-04-01",
        "description": "Test fixture",
        "tables": {"statutory_parameter": rows},
    }


def parameter(code, *, ends=None):
    row = {
        "parameter_code": code,
        "value_numeric": "4.000000",
        "unit": "months",
        "effective_from": "2023-04-01",
        "source_reference": "Test agreement, clause 3",
    }
    if ends:
        row["effective_to"] = ends
    return row


# ------------------------------------------------------------ deriving it


def test_a_fixture_whose_rows_all_close_stops_applying_at_the_latest_one():
    found = scope_of(
        document(
            "X",
            rows=[
                parameter("A", ends="2026-04-01"),
                parameter("B", ends="2025-01-01"),
            ],
        )
    )

    assert found == datetime.date(2026, 4, 1), "the LAST row to close is when the version does"


def test_one_open_ended_row_makes_the_whole_version_open_ended():
    """The safe direction, deliberately. A version wrongly thought closed drops
    out of the payroll gate, and that gate is the only thing standing between
    unverified figures and a payslip — so the doubt resolves towards staying
    in."""
    found = scope_of(document("X", rows=[parameter("A", ends="2026-04-01"), parameter("B")]))

    assert found is None


def test_the_loader_records_it_without_being_asked(db):
    load_reference_data(document("REF-TEST-SCOPE", rows=[parameter("A", ends="2026-04-01")]))

    version = ReferenceDataVersion.objects.get(version_label="REF-TEST-SCOPE")
    assert version.applies_until == datetime.date(2026, 4, 1)


def test_an_open_ended_version_records_nothing(db):
    load_reference_data(document("REF-TEST-OPEN", rows=[parameter("A")]))

    assert ReferenceDataVersion.objects.get(version_label="REF-TEST-OPEN").applies_until is None


# ----------------------------------------------------- and what reads it


def test_the_shipped_2023_agreement_knows_it_has_stopped():
    """Not a synthetic case: these four versions are the ones that were being
    demanded of a verifier for no reachable figure. Asserted against the
    shipped fixtures rather than the dev database, so it holds for a fresh
    clone too."""
    reference = pathlib.Path(__file__).resolve().parents[2] / "reference"
    for name in (
        "ref-2023.04.01-bccci.json",
        "ref-2023.04.01-bccci-rules.json",
        "ref-2023.04.01-bccci-termination.json",
        "ref-2023.04.01-bccci-leave-types.json",
    ):
        found = scope_of(json.loads((reference / name).read_text(encoding="utf-8")))
        assert found == datetime.date(2026, 4, 1), name
