"""A row in force before the instrument it cites existed (D-263).

**Nothing in the corpus fires this**, which is why every case below is
constructed. A check that has never been watched reject anything is a check
nobody has evidence for — this build has shipped six of those — so the tests
here are mostly about making it fire, and one of them is about the corpus being
genuinely clean rather than the parser quietly reading no dates at all.
"""

from __future__ import annotations

import datetime

import pytest
from django.core.management import call_command

from statutory import checks

BCCCI = (
    "Bargaining Council for the Contract Cleaning Services Industry (KZN) Main "
    "Collective Agreement, Notice 1726 of 2023 in GG 48356, 31 March 2023"
)


# --------------------------------------------------------- reading the citation


@pytest.mark.statutory
@pytest.mark.parametrize(
    ("reference", "expected", "how"),
    [
        (BCCCI, datetime.date(2023, 3, 31), "date"),
        (
            "GN R.7083, GG 54075, 3 February 2026 (National Minimum Wage Act 9 of 2018)",
            datetime.date(2026, 2, 3),
            "date",
        ),
        (
            "GN 5970, GG 52232, March 2025 (Basic Conditions of Employment Act 75 of 1997)",
            datetime.date(2025, 3, 1),
            "month",
        ),
        ("Basic Conditions of Employment Act 75 of 1997", datetime.date(1997, 1, 1), "year"),
        (
            "SARS Rates of Tax for Individuals, 2027 tax year (1 March 2026 - 28 February 2027)",
            datetime.date(2026, 3, 1),
            "date",
        ),
    ],
)
def test_the_most_specific_date_present_wins(reference, expected, how):
    """A notice carries its own date AND the year of the Act it is made under.
    Taking the earliest of the two would bound a 2026 notice at 2018 and check
    nothing — which is how a guard comes to pass over everything."""
    found = checks.cited_commencement(reference)

    assert found is not None
    assert found[0] == expected
    assert how in found[1]


@pytest.mark.statutory
@pytest.mark.parametrize(
    "reference",
    [
        "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text",
        "Bargaining council agreement, clause 8",
        "",
    ],
)
def test_a_citation_with_no_date_yields_no_bound(reference):
    """Skipped, not guessed at. A bound nobody can derive is not one anybody
    should invent — the same rule as never inventing a statutory figure."""
    assert checks.cited_commencement(reference) is None


# ----------------------------------------------------- watching the check fire


def a_wage_row(effective_from, reference=BCCCI):
    """A minimum wage row is the cheapest cited, effective-dated row to build."""
    from statutory.models import MinimumWageRate, Sector

    sector, _ = Sector.objects.get_or_create(
        code="CONTRACT_CLEANING", defaults={"name": "Contract cleaning"}
    )
    return MinimumWageRate.objects.create(
        sector=sector,
        effective_from=effective_from,
        hourly_rate="32.40",
        source_reference=reference,
    )


@pytest.mark.statutory
def test_a_row_effective_before_its_own_gazette_is_refused(db):
    """THE CASE THIS EXISTS FOR. The 2023 agreement was gazetted on 31 March
    2023; a row effective 1 March 2023 under that citation is either a
    mistyped date or the wrong instrument, and on a payslip it is a plausible
    number in a plausible column."""
    a_wage_row(datetime.date(2023, 3, 1))

    issues = checks.check_commencement()

    assert len(issues) == 1
    assert issues[0].blocking is True, "arithmetic, not a resemblance — this refuses"
    assert "01 March 2023" in issues[0].message
    assert "31 March 2023" in issues[0].message
    assert "BEFORE" in issues[0].message
    assert "Notice 1726 of 2023" in issues[0].message, "name the instrument, or nobody can check"
    assert issues[0] in checks.run_all(), "not wired into run_all is not wired in at all"


@pytest.mark.statutory
def test_verifystatutory_refuses_while_it_stands(db):
    """run_all() is what gates the command, so a blocking issue here must
    actually stop a version being recorded as verified."""
    from django.core.management.base import CommandError

    from core.models import AppUser
    from statutory.models import ReferenceDataVersion

    a_wage_row(datetime.date(2023, 3, 1))
    version = ReferenceDataVersion.objects.create(
        version_label="REF-TEST-COMMENCE", applies_from=datetime.date(2023, 3, 1)
    )
    person = AppUser.objects.create_user(email="kobus@example.com", password="x" * 16)

    with pytest.raises(CommandError) as caught:
        call_command(
            "verifystatutory",
            version.version_label,
            "--verified-by",
            person.email,
            "--current-through",
            "2024-02-29",
            "--golden-tests-passed",
        )

    assert "does not reconcile" in str(caught.value)
    version.refresh_from_db()
    assert version.verified_at is None


@pytest.mark.statutory
@pytest.mark.parametrize(
    "effective_from",
    [datetime.date(2023, 3, 31), datetime.date(2023, 4, 1), datetime.date(2026, 3, 1)],
)
def test_on_the_day_and_after_are_both_fine(db, effective_from):
    """Watched NOT firing. Equality is the ordinary case, not an edge one: the
    Van Wyk reading-in is effective the day judgment was handed down."""
    a_wage_row(effective_from)

    assert checks.check_commencement() == []


@pytest.mark.statutory
def test_a_row_whose_citation_carries_no_date_is_not_reported(db):
    a_wage_row(datetime.date(1999, 1, 1), reference="Sectoral Determination 1, consolidated text")

    assert checks.check_commencement() == []


# ------------------------------------------------------------------ the corpus


@pytest.mark.statutory
def test_the_whole_loaded_corpus_reconciles(db):
    """Nothing in eighteen source documents is effective before its own
    instrument — and this asserts the parser found dates to check against,
    because "no issues" over "no dates parsed" would read identically."""
    call_command("loadstatutory", "--all")

    from statutory import verification

    dated = {
        line.document
        for line in verification.lines()
        if checks.cited_commencement(line.document) is not None
    }

    assert checks.check_commencement() == []
    assert len(dated) >= 9, (
        f"only {len(dated)} source documents yielded a date to check against, so a "
        f"clean result would mean the parser stopped reading rather than the data "
        f"being right"
    )
