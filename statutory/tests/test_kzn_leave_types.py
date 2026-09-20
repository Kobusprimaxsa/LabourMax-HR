"""Study leave, shop steward leave and the prenatal clinic day (D-268).

Three entitlements the BCCCI Main Agreement CREATES. None of them is in the
BCEA, so they exist for a KwaZulu-Natal contract cleaner and for nobody else
this system serves — which is why the figures are area-scoped reference data
and the catalogue rows carry no quantum at all.

**None of the three is a per-cycle bank**, and that is the reason each is a
``statutory_parameter`` rather than a ``leave_rule_set`` column:

* study leave is per EXAMINATION — one day to prepare, one day to write;
* shop steward leave is per YEAR but at one of two figures, chosen by a fact
  about the person that no column carries;
* the prenatal day is per PREGNANCY — one day in each of the three months
  before the expected date of confinement.

The prenatal figures were loaded in D-244 and **read by nothing** until the
resolver below existed — the same shape of gap D-243 left behind with the
long-service band, found while seeding the leave type that spends them.
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

FIXTURES = (
    "ref-2023.04.01-bccci-leave-types.json",
    "ref-2023.04.01-bccci-rules.json",
    "ref-2026.04.01-bccci-leave-types.json",
    "ref-2026.04.01-bccci-maternity.json",
)

IN_2023 = datetime.date(2025, 6, 1)
IN_2026 = datetime.date(2026, 6, 1)


@pytest.fixture
def area_b(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    area = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    for name in FIXTURES:
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))
    return sector, area


@pytest.fixture
def domestic(db):
    """An employer nothing in this file applies to. Every "not granted" answer
    below is checked against a real sector, not against an empty database."""
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


# ----------------------------------------------------------------- study leave


@pytest.mark.parametrize("on_date", [IN_2023, IN_2026])
def test_study_leave_is_two_separate_days_per_examination(area_b, on_date):
    """Clause 12.1 (predecessor clause 11.1): one day to prepare and one day to
    write, each examination, on full pay. Stored apart because an employee who
    writes without preparing is owed the second and not the first."""
    sector, area = area_b

    answer = resolve.study_leave(sector, on_date, area)

    assert answer.granted is True
    assert answer.prepare_days_per_examination == Decimal("1.000000")
    assert answer.write_days_per_examination == Decimal("1.000000")
    assert answer.days_per_examination == Decimal("2.000000")
    assert "Main Collective Agreement" in answer.source_reference


def test_study_leave_cites_the_predecessors_own_clause_number(area_b):
    """D-260: the predecessor numbers study leave 11 and the successor 12.
    Citing one by the other's number points a verifier at the wrong page."""
    sector, area = area_b

    assert "clause 11.1(a)" in resolve.study_leave(sector, IN_2023, area).source_reference
    assert "clause 12.1(a)" in resolve.study_leave(sector, IN_2026, area).source_reference


def test_study_leave_is_not_granted_to_a_domestic_worker(domestic):
    """Watched NOT firing. The BCEA has no study leave, so this is the ordinary
    answer for every employer except one — which is why the resolver returns a
    state instead of raising."""
    answer = resolve.study_leave(domestic, IN_2026)

    assert answer.granted is False
    assert answer.days_per_examination is None


# ---------------------------------------------------------- shop steward leave


@pytest.mark.parametrize("on_date", [IN_2023, IN_2026])
@pytest.mark.parametrize(
    ("is_office_bearer", "expected"),
    [(True, Decimal("4.000000")), (False, Decimal("6.000000"))],
)
def test_shop_steward_leave_answers_the_declared_status(
    area_b, on_date, is_office_bearer, expected
):
    """Clause 20.4(a): 4 days for an office bearer of a representative trade
    union, 6 for any other shop steward. **The office bearer's figure is the
    SMALLER one**, which reads like the gazette swapped its limbs — both
    agreements print it that way, so it is answered as printed (O-32)."""
    sector, area = area_b

    answer = resolve.shop_steward_leave(
        sector, on_date, is_office_bearer=is_office_bearer, sector_area=area
    )

    assert answer.granted is True
    assert answer.days_per_year == expected
    assert answer.is_office_bearer is is_office_bearer


def test_an_office_bearer_gets_fewer_days_than_an_ordinary_shop_steward(area_b):
    """Named so nobody "corrects" it later. Asserted as a relationship rather
    than two separate figures, because the surprising thing is the direction."""
    sector, area = area_b

    bearer = resolve.shop_steward_leave(sector, IN_2026, is_office_bearer=True, sector_area=area)
    other = resolve.shop_steward_leave(sector, IN_2026, is_office_bearer=False, sector_area=area)

    assert bearer.days_per_year < other.days_per_year


def test_shop_steward_leave_is_not_granted_to_a_domestic_worker(domestic):
    answer = resolve.shop_steward_leave(domestic, IN_2026, is_office_bearer=False)

    assert answer.granted is False
    assert answer.days_per_year is None


# ------------------------------------------------------------- prenatal clinic


@pytest.mark.parametrize("on_date", [IN_2023, IN_2026])
def test_the_prenatal_clinic_day_finally_has_a_reader(area_b, on_date):
    """Clause 13.2 (predecessor 12.2): one day's fully paid leave in EACH of
    the 3 months before the expected date of confinement. Loaded in D-244 and
    read by nothing until now."""
    sector, area = area_b

    answer = resolve.prenatal_clinic_leave(sector, on_date, area)

    assert answer.granted is True
    assert answer.paid_days_per_month == Decimal("1.000000")
    assert answer.months_before_birth == 3
    assert answer.total_days == Decimal("3.000000")


def test_the_prenatal_clinic_day_is_not_granted_to_a_domestic_worker(domestic):
    answer = resolve.prenatal_clinic_leave(domestic, IN_2026)

    assert answer.granted is False
    assert answer.total_days is None


# -------------------------------------------- the two agreements, side by side


def test_all_three_entitlements_are_unchanged_across_the_1_april_2026_boundary(area_b):
    """D-265's finding extended to the three figures this chunk loads: the
    hourly wage is still the only thing that moves between the two agreements.
    Asserted rather than assumed — a figure assumed unchanged is a figure
    nobody checked."""
    sector, area = area_b
    last_day = datetime.date(2026, 3, 31)
    first_day = datetime.date(2026, 4, 1)

    for before, after in (
        (resolve.study_leave(sector, last_day, area), resolve.study_leave(sector, first_day, area)),
        (
            resolve.prenatal_clinic_leave(sector, last_day, area),
            resolve.prenatal_clinic_leave(sector, first_day, area),
        ),
    ):
        assert before.granted and after.granted

    assert (
        resolve.study_leave(sector, last_day, area).days_per_examination
        == resolve.study_leave(sector, first_day, area).days_per_examination
    )
    assert (
        resolve.prenatal_clinic_leave(sector, last_day, area).total_days
        == resolve.prenatal_clinic_leave(sector, first_day, area).total_days
    )
    for is_office_bearer in (True, False):
        assert (
            resolve.shop_steward_leave(
                sector, last_day, is_office_bearer=is_office_bearer, sector_area=area
            ).days_per_year
            == resolve.shop_steward_leave(
                sector, first_day, is_office_bearer=is_office_bearer, sector_area=area
            ).days_per_year
        )


def test_nothing_answers_before_the_predecessor_took_effect(area_b):
    """1 April 2023 is where the predecessor starts. A date before it has no
    instrument, and the resolver says so rather than reaching forward."""
    sector, area = area_b

    assert resolve.study_leave(sector, datetime.date(2023, 3, 31), area).granted is False
    assert (
        resolve.shop_steward_leave(
            sector, datetime.date(2023, 3, 31), is_office_bearer=True, sector_area=area
        ).granted
        is False
    )
