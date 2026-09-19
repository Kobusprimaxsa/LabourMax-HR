"""One instrument, one citation — and the guard that says when it is two.

The BCCCI Main Agreement was cited two ways and the SARS code guide two ways
(O-33). The verification workbook groups by SOURCE DOCUMENT, so each pair read
as two documents, and a person could verify one spelling to completion with the
version still showing incomplete and nothing on screen explaining why.

The citations are normalised in the fixtures (D-257). This is the guard that
catches the next one, and the reason it WARNS rather than refuses: a genuine
revision of a guide is two documents and a real case.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import checks
from statutory.loader import load_reference_data
from statutory.models import StatutoryParameter

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"

BCCCI = (
    "Bargaining Council for the Contract Cleaning Services Industry (KZN) "
    "Main Collective Agreement, GN R.7296 in GG 54412, 27 March 2026"
)


def a_parameter(code: str, reference: str) -> StatutoryParameter:
    return StatutoryParameter.objects.create(
        parameter_code=code,
        value_numeric=Decimal("1.000000"),
        unit=StatutoryParameter.Unit.RATIO,
        effective_from=datetime.date(2026, 4, 1),
        source_reference=reference,
    )


def messages(issues) -> str:
    return "\n".join(issue.message for issue in issues)


def test_the_loaded_data_has_no_duplicate_spellings_left(db):
    """The state O-33 was closed into. Every shipped fixture, loaded, and no
    two source documents naming the same instrument."""
    from statutory.loader import FIXTURE_ORDER

    for name in FIXTURE_ORDER:
        path = REFERENCE / name
        if path.exists():
            load_reference_data(json.loads(path.read_text(encoding="utf-8")))

    assert checks.check_citation_spellings() == []


def test_two_spellings_of_one_gazette_notice_are_reported(db):
    """The BCCCI case, reintroduced. Same GN and same GG, two strings."""
    a_parameter("FIRST", f"{BCCCI}, clause 4.1(a)(i)")
    a_parameter(
        "SECOND",
        "BCCCI (KwaZulu-Natal) Main Collective Agreement, GN R.7296 in GG 54412, "
        "27 March 2026, clause 21.1(b)(i)",
    )

    issues = checks.check_citation_spellings()

    assert len(issues) == 1
    assert issues[0].blocking is False, "a genuine revision is a real case; this cannot decide"
    assert "may be the same instrument cited twice" in issues[0].message
    assert "Bargaining Council" in issues[0].message
    assert "BCCCI (KwaZulu-Natal)" in issues[0].message


def test_two_spellings_of_one_sars_guide_code_are_reported(db):
    """The other half of O-33: PAYE-AE-06-G06 written two ways."""
    a_parameter(
        "FIRST",
        "SARS Guide for Codes Applicable to Employees Tax Certificates "
        "(PAYE-AE-06-G06), 2026 issue",
    )
    a_parameter(
        "SECOND",
        "SARS PAYE-AE-06-G06, Guide for Codes Applicable to Employees Tax "
        "Certificates 2026, revision 13, effective 19 September 2025",
    )

    issues = checks.check_citation_spellings()

    assert len(issues) == 1
    assert "PAYE-AE-06-G06" in issues[0].message


# ------------------------------------------------- what it must NOT report on


def test_two_notices_under_one_act_are_two_documents(db):
    """Watched NOT firing, and this is the case that decides the design. GN 5970
    and GN 7384 are both made under BCEA s6(3) and both name the Act. They are
    two determinations, two documents, and two evenings — reporting them would
    train the reader to ignore this check."""
    a_parameter(
        "FIRST",
        "GN 5970, GG 52232, March 2025 (Basic Conditions of Employment Act 75 of 1997, s6(3))",
    )
    a_parameter(
        "SECOND",
        "GN 7384, GG 54544, 17 April 2026 (Basic Conditions of Employment Act 75 of 1997, s6(3))",
    )

    assert checks.check_citation_spellings() == []


def test_an_act_and_a_notice_made_under_it_are_two_documents(db):
    """The Act is a document; so is a notice published under it. An Act number
    identifies the ACT, never a thing made under it."""
    a_parameter("FIRST", "Basic Conditions of Employment Act 75 of 1997, s37(1)(a)")
    a_parameter(
        "SECOND",
        "GN 5970, GG 52232, March 2025 (Basic Conditions of Employment Act 75 of 1997, s6(3))",
    )

    assert checks.check_citation_spellings() == []


def test_two_spellings_of_one_act_with_no_gazette_anywhere_are_reported(db):
    """The weak identifier still decides when there is nothing stronger — two
    ways of writing the same Act, neither carrying a notice number."""
    a_parameter("FIRST", "Basic Conditions of Employment Act 75 of 1997, s37(1)(a)")
    a_parameter(
        "SECOND",
        "Basic Conditions of Employment Act 75 of 1997 (consolidated text), section 37(1)(b)",
    )

    issues = checks.check_citation_spellings()

    assert len(issues) == 1
    assert "Act 75 of 1997" in issues[0].message
    assert "consolidated text" in issues[0].message


def test_unrelated_documents_are_not_reported(db):
    a_parameter("FIRST", "Skills Development Levies Act 9 of 1999, s3(1)")
    a_parameter("SECOND", "Value-Added Tax Act 89 of 1991, s7(1)(a)")

    assert checks.check_citation_spellings() == []


def test_the_check_runs_as_part_of_checkstatutory(db):
    """A guard nothing calls is a guard that does nothing (the P0 five)."""
    import inspect

    source = inspect.getsource(checks.run_all)
    assert "check_citation_spellings" in source
