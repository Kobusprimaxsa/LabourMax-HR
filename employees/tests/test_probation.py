"""Probation: the column nothing read, and the cap that now bounds it (D-277).

``employee_engagement.probation_end_date`` has been in the schema since P4 and
was never read by anything. That was harmless until BCCCI clause 21.1(b) made
it the fact that chooses between one week's notice and two — at which point a
column nobody fills in becomes a wrong notice period.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from core.managers import tenant_context
from core.models import Tenant
from employees import probation
from employees.engagements import MINIMUM_AGE_PARAMETER, EngagementRefusedError, engage
from employees.identity import luhn_check_digit
from employees.models import Employee
from employers.models import Employer
from statutory.loader import load_reference_data
from statutory.models import Sector, SectorArea, StatutoryParameter

pytestmark = pytest.mark.django_db

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"
START = datetime.date(2026, 4, 1)


class FakeEngagement:
    """Two dates and nothing else — ``is_on_probation`` reads no other column,
    and a real engagement would drag a tenant context in for no benefit."""

    def __init__(self, start_date, probation_end_date):
        self.start_date = start_date
        self.probation_end_date = probation_end_date


# ------------------------------------------------------ reading the column


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        (datetime.date(2026, 4, 1), True),
        (datetime.date(2026, 6, 30), True),
        # THE BOUNDARY, and it is a reading rather than a fact the column
        # carries: the last day of probation is a day ON probation, the way
        # termination_date is the last day of service.
        (datetime.date(2026, 7, 1), True),
        (datetime.date(2026, 7, 2), False),
        (datetime.date(2026, 3, 31), False),
    ],
)
def test_the_last_day_of_probation_is_a_day_on_probation(on_date, expected):
    engagement = FakeEngagement(START, datetime.date(2026, 7, 1))

    assert probation.is_on_probation(engagement, on_date) is expected


def test_no_probation_date_means_not_on_probation():
    """An employer who captured nothing did not put the employee on probation.
    Inferring one from a blank field would hand them the SHORTER notice period
    on the strength of an empty cell."""
    assert probation.is_on_probation(FakeEngagement(START, None), START) is False
    assert probation.is_on_probation(None, START) is False


# ---------------------------------------------------------------- the cap


@pytest.fixture
def area_b(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    area = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    load_reference_data(
        json.loads((REFERENCE / "ref-2023.04.01-bccci-probation.json").read_text(encoding="utf-8"))
    )
    return sector, area


def test_four_months_exactly_is_permitted(area_b):
    sector, area = area_b

    probation.check_probation(
        start_date=START,
        probation_end_date=datetime.date(2026, 8, 1),
        sector=sector,
        sector_area=area,
    )


def test_a_day_over_four_months_is_refused_and_names_the_clause(area_b):
    """Refuses rather than warns: a probation term longer than the agreement
    permits is a breach on the day it is captured, and the wrong moment to find
    out is at termination, when the notice period is being read off it."""
    sector, area = area_b

    with pytest.raises(probation.ProbationRefusedError) as raised:
        probation.check_probation(
            start_date=START,
            probation_end_date=datetime.date(2026, 8, 2),
            sector=sector,
            sector_area=area,
        )

    message = str(raised.value)
    assert "01 August 2026" in message, "say the latest date it MAY run to"
    assert "4 months" in message
    assert "clause 3" in message, "name the clause, so the employer can read it"


def test_an_employer_under_no_cap_is_not_told_there_is_one(db):
    """Watched NOT firing. Nothing in the BCEA caps probation, and the LRA's
    Code of Good Practice asks only that it be reasonable — which is not a
    number and must not be invented as one. A domestic employer putting someone
    on six months' probation is doing nothing this system may refuse."""
    Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")

    probation.check_probation(
        start_date=START,
        probation_end_date=datetime.date(2026, 12, 1),
        sector=Sector.objects.get(code=Sector.Code.DOMESTIC),
    )


def test_probation_ending_before_the_engagement_starts_is_refused(db):
    with pytest.raises(probation.ProbationRefusedError) as raised:
        probation.check_probation(start_date=START, probation_end_date=datetime.date(2026, 3, 1))

    assert "before the engagement starts" in str(raised.value)


# -------------------------------------------------- and at the engagement


@pytest.fixture
def cleaner(area_b):
    """A KwaZulu-Natal contract cleaner, whose employer the cap binds."""
    sector, area = area_b
    StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=15,
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s43(1)",
    )
    tenant = Tenant.objects.create(trading_name="Subscriber")
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name="Cleaning co", sector=sector, sector_area=area
        )
        body = "9001015009" + "08"
        return Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Sipho",
            last_name="Ndlovu",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000002",
            email="sipho@example.com",
            id_number=body + str(luhn_check_digit(body)),
        )


def test_engage_refuses_a_probation_longer_than_the_agreement_allows(cleaner):
    """The cap is checked BEFORE the transaction opens, like the age rule, so a
    refusal writes nothing at all."""
    employee = cleaner

    with pytest.raises(EngagementRefusedError) as raised:
        engage(
            employee,
            start_date=START,
            job_title="Cleaner",
            probation_end_date=datetime.date(2027, 1, 1),
        )

    assert "clause 3" in str(raised.value)
    with tenant_context(employee.tenant_id):
        assert not employee.engagements.exists(), "a refusal writes nothing"
