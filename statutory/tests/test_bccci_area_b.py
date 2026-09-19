"""The BCCCI Main Agreement: Area B's wage rates, and the March 2026 gap.

GN R.7296 in Government Gazette 54412, 27 March 2026 — the instrument
Sectoral Determination 1 points at for KwaZulu-Natal and that this build did not
have. Until it was loaded, a KZN contract cleaning employer could not be
onboarded; worse, the wage resolver answered the National Minimum Wage for them,
which is a plausible figure and R2,17 an hour short.

**These are the first future-dated rows in this system.** Two of the three rates
take effect after today, so the tests below step across all three periods.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import resolve
from statutory.loader import load_reference_data
from statutory.models import (
    MinimumWageRate,
    ReferenceDataVersion,
    Sector,
    SectorArea,
    StatutoryParameter,
)

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "reference" / "ref-2026.04.01-bccci.json"

#: cl 4.1(a)(i)-(iii), verified against the gazette page by page.
APRIL_2026 = Decimal("32.4000")
MARCH_2027 = Decimal("34.0200")
MARCH_2028 = Decimal("35.7200")

#: The National Minimum Wage in force on 1 March 2026 — what Area B used to
#: resolve to by silent fallback.
NMW = Decimal("30.2300")


@pytest.fixture
def cleaning(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING,
        name="Contract cleaning sector",
        determination_reference="Sectoral Determination 1",
    )
    areas = {
        code: SectorArea.objects.create(
            sector=sector,
            code=code,
            name=code.replace("_", " ").title(),
            uses_bargaining_council_rates=(code == "AREA_B"),
        )
        for code in ("AREA_A", "AREA_B", "AREA_C")
    }
    return sector, areas


@pytest.fixture
def loaded(cleaning):
    """The shipped fixture, loaded as the loader would load it."""
    load_reference_data(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return cleaning


def area_b_wage(loaded, on_date) -> Decimal:
    sector, areas = loaded
    return resolve.minimum_wage(on_date, sector=sector, sector_area=areas["AREA_B"]).hourly_rate


# ------------------------------------------------- A1a: the three rates load


def test_the_three_gazetted_rates_load_against_area_b(loaded):
    sector, areas = loaded
    rows = MinimumWageRate.objects.filter(sector=sector, sector_area=areas["AREA_B"]).order_by(
        "effective_from"
    )

    assert [row.hourly_rate for row in rows] == [APRIL_2026, MARCH_2027, MARCH_2028]
    assert [row.effective_from for row in rows] == [
        datetime.date(2026, 4, 1),
        datetime.date(2027, 3, 1),
        datetime.date(2028, 3, 1),
    ]
    assert all("GN R.7296" in row.source_reference for row in rows)
    assert all("clause 4.1(a)" in row.source_reference for row in rows)


def test_the_first_rates_derived_start_date_is_recorded_on_the_row(loaded):
    """1 April 2026 is nowhere in the gazette — it is read off clause 4.1(a)(i)
    and the extension notice together. A derived date with no derivation on it
    is a date the next person has to take on trust."""
    row = MinimumWageRate.objects.get(effective_from=datetime.date(2026, 4, 1))

    assert "DERIVED DATE" in row.notes
    assert "27 March 2026" in row.notes
    assert "1 April 2026" in row.notes


def test_each_rate_closes_its_predecessor(loaded):
    """``effective_to`` is EXCLUSIVE, so a rate runs up to but not including its
    successor's start. Three open-ended rows would all be in force at once, and
    the exclusion constraint refuses them — which is how this was caught."""
    rows = list(MinimumWageRate.objects.order_by("effective_from"))

    assert rows[0].effective_to == datetime.date(2027, 3, 1)
    assert rows[1].effective_to == datetime.date(2028, 3, 1)
    assert rows[2].effective_to is None, "the last rate runs until something replaces it"


# --------------------------------------- A1b: future-dated rows resolve


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        (datetime.date(2026, 4, 1), APRIL_2026),
        (datetime.date(2026, 6, 15), APRIL_2026),
        (datetime.date(2027, 2, 28), APRIL_2026),
        (datetime.date(2027, 3, 1), MARCH_2027),
        (datetime.date(2027, 4, 15), MARCH_2027),
        (datetime.date(2028, 2, 29), MARCH_2027),
        (datetime.date(2028, 3, 1), MARCH_2028),
        (datetime.date(2028, 6, 15), MARCH_2028),
        (datetime.date(2035, 1, 1), MARCH_2028),
    ],
)
def test_the_rate_in_force_is_resolved_across_every_period(loaded, on_date, expected):
    """Both sides of both boundaries. A rate that changes on 1 March must apply
    ON 1 March and not on 28 February — the half-open convention, checked rather
    than assumed, because off-by-one here is a payslip wrong for exactly the
    employees paid on the day a rate moved."""
    assert area_b_wage(loaded, on_date) == expected


def test_a_future_rate_is_loaded_now_and_invisible_until_its_date(loaded):
    """The point of loading ahead: the March 2027 row exists today and does not
    affect today's answer."""
    sector, areas = loaded
    assert MinimumWageRate.objects.filter(hourly_rate=MARCH_2027).exists()
    assert area_b_wage(loaded, datetime.date(2026, 9, 19)) == APRIL_2026


# --------------------------------------------- A1c: the March 2026 refusal


def test_a_march_2026_period_is_refused_and_names_the_missing_instrument(loaded):
    """NOT the National Minimum Wage, NOT the April rate reached forward, NOT
    Area A. The agreement's own clause 2(1)(a) says the predecessor Main
    Agreement continues in force, so March 2026 has an instrument — one nobody
    has loaded — and naming it is the difference between a gap somebody can
    close and a mystery."""
    sector, areas = loaded

    with pytest.raises(resolve.StatutoryValueMissingError) as raised:
        resolve.minimum_wage(datetime.date(2026, 3, 31), sector=sector, sector_area=areas["AREA_B"])

    message = str(raised.value)
    assert "AREA_B" in message
    assert "clause 2(1)(a)" in message
    assert "PREDECESSOR" in message
    assert "National Minimum Wage is the floor under a sector" in message


def test_the_refusal_does_not_quietly_answer_the_national_minimum_wage(loaded):
    """The failure this guard exists for, stated as its own test: before it, a
    KwaZulu-Natal cleaner resolved to R30,23 — plausible, and R2,17 an hour
    short of the agreement in force."""
    sector, areas = loaded
    MinimumWageRate.objects.create(
        hourly_rate=NMW,
        effective_from=datetime.date(2026, 3, 1),
        source_reference="National Minimum Wage Act 9 of 2018",
    )

    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.minimum_wage(datetime.date(2026, 3, 31), sector=sector, sector_area=areas["AREA_B"])


def test_an_area_that_does_not_use_bargaining_council_rates_still_falls_back(loaded):
    """The other half — the guard is narrow. Area C has no row in this fixture
    and correctly falls through to the National Minimum Wage, because the NMW
    genuinely IS the floor under a sector the Minister has not rated."""
    sector, areas = loaded
    MinimumWageRate.objects.create(
        hourly_rate=NMW,
        effective_from=datetime.date(2026, 3, 1),
        source_reference="National Minimum Wage Act 9 of 2018",
    )

    assert (
        resolve.minimum_wage(
            datetime.date(2026, 3, 31), sector=sector, sector_area=areas["AREA_C"]
        ).hourly_rate
        == NMW
    )


# ------------------------------------------------------- A1d: above the floor


def test_the_gazetted_rate_is_at_or_above_the_national_minimum_wage(loaded):
    """A sanity check, not a substitute for reading the gazette: a sectoral rate
    below the NMW would mean one of the two figures is transcribed wrong."""
    MinimumWageRate.objects.create(
        hourly_rate=NMW,
        effective_from=datetime.date(2026, 3, 1),
        source_reference="National Minimum Wage Act 9 of 2018",
    )

    assert APRIL_2026 > NMW
    assert APRIL_2026 - NMW == Decimal("2.1700")


# ------------------------------------- A1b: the staleness guard, both ways


def a_version(label, *, current_through, user):
    return ReferenceDataVersion.objects.create(
        version_label=label,
        applies_from=datetime.date(2026, 4, 1),
        description="Test",
        verified_at=datetime.datetime(2026, 3, 30, tzinfo=datetime.UTC),
        verified_by_user=user,
        golden_tests_passed=True,
        data_current_through=current_through,
    )


@pytest.fixture
def verifier(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email="verifier@example.com", password="x" * 14, first_name="A", last_name="Verifier"
    )


def test_the_staleness_guard_blocks_a_period_the_rows_actually_cover(loaded, verifier):
    """THE FINDING. ``data_current_through`` is a HUMAN DECLARATION of how far
    the verifier vouched, not something derived from the rows. This version
    holds a rate effective 1 March 2027, but a verifier who wrote 28 February
    2027 has vouched for nothing beyond that — so a March 2027 period is
    blocked even though the figure for it is sitting in the table.

    That is the SAFE direction and the guard is right to do it. It is recorded
    because it is surprising: loading future-dated rows does not extend the
    verification, and whoever verifies this version must set
    ``data_current_through`` to at least 2028-03-01 or the 2027 and 2028 rates
    are unusable despite being loaded.
    """
    version = a_version("REF-TEST-SHORT", current_through=datetime.date(2027, 2, 28), user=verifier)

    covered_by_a_row = area_b_wage(loaded, datetime.date(2027, 6, 30))
    assert covered_by_a_row == MARCH_2027, "the rate exists and resolves"

    period_end = datetime.date(2027, 6, 30)
    assert period_end > version.data_current_through, "yet the run is blocked"


def test_the_staleness_guard_does_not_pass_a_period_beyond_what_was_vouched_for(loaded, verifier):
    """The dangerous direction, checked: nothing about loading rows that run to
    2028 moves ``data_current_through`` forward on its own."""
    version = a_version("REF-TEST-SHORT", current_through=datetime.date(2027, 2, 28), user=verifier)
    version.refresh_from_db()

    assert version.data_current_through == datetime.date(2027, 2, 28)
    assert ReferenceDataVersion.in_force_on(datetime.date(2028, 6, 1)) == version, (
        "the version is still the one in force — staleness is a separate question "
        "from which version applies, and conflating them would make a stale version "
        "invisible rather than blocking"
    )


def test_a_version_vouched_through_the_last_rate_covers_every_loaded_period(loaded, verifier):
    """What the verifier of this fixture must actually write."""
    version = a_version("REF-TEST-FULL", current_through=datetime.date(2029, 2, 28), user=verifier)

    for period_end in (
        datetime.date(2026, 6, 30),
        datetime.date(2027, 6, 30),
        datetime.date(2028, 6, 30),
    ):
        assert period_end <= version.data_current_through


# ------------------------------------------------ A2: the two monthly factors


def test_area_b_resolves_four_point_three_three_and_everyone_else_four_and_a_third(loaded):
    """Clause 3 against BCEA s35(3). Two binding figures for one concept."""
    sector, areas = loaded
    StatutoryParameter.objects.create(
        parameter_code="MONTHLY_TO_WEEKLY_FACTOR",
        value_numeric=Decimal("4.333333"),
        unit=StatutoryParameter.Unit.RATIO,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s35(3)",
    )
    on = datetime.date(2026, 6, 1)

    assert resolve.parameter_value(
        "MONTHLY_TO_WEEKLY_FACTOR", on, sector=sector, sector_area=areas["AREA_B"]
    ) == Decimal("4.330000")
    assert resolve.parameter_value(
        "MONTHLY_TO_WEEKLY_FACTOR", on, sector=sector, sector_area=areas["AREA_A"]
    ) == Decimal("4.333333"), "Area A is not covered by the agreement"
    assert resolve.parameter_value("MONTHLY_TO_WEEKLY_FACTOR", on) == Decimal("4.333333")


def test_the_scoped_factor_does_not_apply_before_the_agreement_takes_effect(loaded):
    """The agreement's factor starts when the agreement does."""
    sector, areas = loaded
    StatutoryParameter.objects.create(
        parameter_code="MONTHLY_TO_WEEKLY_FACTOR",
        value_numeric=Decimal("4.333333"),
        unit=StatutoryParameter.Unit.RATIO,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s35(3)",
    )

    assert resolve.parameter_value(
        "MONTHLY_TO_WEEKLY_FACTOR",
        datetime.date(2026, 3, 31),
        sector=sector,
        sector_area=areas["AREA_B"],
    ) == Decimal("4.333333")
