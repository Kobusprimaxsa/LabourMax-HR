"""Capturing pay — P4's definition of done.

*Capturing an employee below the sectoral minimum raises a visible, logged
exception, and a future-dated increase flips the cache on its own effective date.*

Both halves are here. The second is the subtler one: the cache on ``employee`` is
what the employee list groups and sorts by, and a rate captured in March to start in
July must not move it in March — nor must it still be showing June's pay group on the
morning of 1 July.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeRemuneration
from employees.remuneration import (
    MONTHLY_FACTOR_PARAMETER,
    BelowMinimumWageError,
    RemunerationRefusedError,
    capture,
    check_minimum_wage,
    rate_in_force,
    refresh_pay_cache,
)
from employers.models import Employer, PayGroup
from statutory.models import MinimumWageRate, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)

#: The gazetted National Minimum Wage from 1 March 2026 (GN R.7083, GG 54075).
NMW_HOURLY = Decimal("30.2300")


def make_id(sequence="5009"):
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def parameters(db):
    for code, value, unit in (
        (MINIMUM_AGE_PARAMETER, "15.000000", StatutoryParameter.Unit.YEARS),
        (MONTHLY_FACTOR_PARAMETER, "4.333333", StatutoryParameter.Unit.RATIO),
    ):
        StatutoryParameter.objects.create(
            parameter_code=code,
            value_numeric=Decimal(value),
            unit=unit,
            effective_from=datetime.date(1997, 12, 1),
            source_reference="Basic Conditions of Employment Act 75 of 1997",
        )


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def minimum_wage(db, sector):
    return MinimumWageRate.objects.create(
        sector=None,
        hourly_rate=NMW_HOURLY,
        effective_from=START,
        source_reference="GN R.7083 in Government Gazette 54075, 3 February 2026",
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Subscriber")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)


@pytest.fixture
def pay_group(db, tenant, employer):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly staff",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=START,
        )


@pytest.fixture
def employee(db, tenant, employer, parameters):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=BORN,
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=make_id(),
        )
    engage(person, start_date=START, job_title="Domestic worker")
    return person


@pytest.fixture
def acknowledger(db):
    return AppUser.objects.create_user(email="owner@example.com", password="x" * 16)


# ------------------------------------------------------------------- capture


def test_capturing_a_rate_stores_all_three_derived_figures(employee, pay_group, minimum_wage):
    row = capture(employee, pay_basis="monthly", rate_amount=Decimal("6000"), effective_from=START)

    assert row.derived_monthly_rate == Decimal("6000.000000")
    assert row.derived_hourly_rate > 0
    assert row.derived_daily_rate > 0
    assert row.minimum_wage_rate_id == minimum_wage.pk, (
        "The floor it was measured against is stored, so the question can be answered "
        "later without re-deriving against whatever is loaded then."
    )


def test_the_working_pattern_defaults_from_the_pay_group(employee, pay_group, minimum_wage):
    row = capture(employee, pay_basis="monthly", rate_amount=Decimal("6000"), effective_from=START)
    assert row.hours_per_day == pay_group.default_hours_per_day
    assert row.days_per_week == pay_group.default_days_per_week


def test_capturing_without_an_open_engagement_is_refused(
    tenant, employer, pay_group, minimum_wage, parameters
):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Not",
            last_name="Engaged",
            date_of_birth=BORN,
            mobile_number="+27820000077",
            email="ne@example.com",
            id_number=make_id(sequence="5077"),
        )

    with pytest.raises(RemunerationRefusedError) as caught:
        capture(person, pay_basis="monthly", rate_amount=Decimal("6000"), effective_from=START)
    assert "no open engagement" in str(caught.value)


def test_a_rate_before_the_engagement_started_is_refused(employee, pay_group, minimum_wage):
    with pytest.raises(RemunerationRefusedError) as caught:
        capture(
            employee,
            pay_basis="monthly",
            rate_amount=Decimal("6000"),
            effective_from=datetime.date(2026, 1, 1),
        )
    assert "before the engagement began" in str(caught.value)


# --------------------------------------------------------------- minimum wage


def test_a_rate_below_the_minimum_raises_with_both_figures(employee, pay_group, minimum_wage):
    """P4's definition of done: a visible, logged exception, not a silent flag."""
    with pytest.raises(BelowMinimumWageError) as caught:
        capture(employee, pay_basis="hourly", rate_amount=Decimal("25.00"), effective_from=START)

    error = caught.value
    assert error.floor == NMW_HOURLY
    assert error.offered == Decimal("25.000000")
    assert "30.2300" in str(error)
    assert "Government Gazette 54075" in str(error), "The message cites the gazette."

    with tenant_context(employee.tenant_id):
        assert EmployeeRemuneration.objects.count() == 0, "A refused capture writes nothing."


def test_the_rate_can_be_accepted_and_the_acceptor_is_recorded(
    employee, pay_group, minimum_wage, acknowledger
):
    """A flag, not a block — the workbook's call, and the right one.

    An employer part-way through correcting a typo must not be locked out of their
    own record. But "the system let me" is not a defence at the CCMA, so who accepted
    it is stored next to the rate.
    """
    row = capture(
        employee,
        pay_basis="hourly",
        rate_amount=Decimal("25.00"),
        effective_from=START,
        acknowledged_by=acknowledger,
    )

    assert row.is_below_minimum is True
    assert row.below_minimum_ack_by_user_id == acknowledger.pk


def test_a_rate_that_clears_records_no_acknowledgement(
    employee, pay_group, minimum_wage, acknowledger
):
    row = capture(
        employee,
        pay_basis="hourly",
        rate_amount=Decimal("35.00"),
        effective_from=START,
        acknowledged_by=acknowledger,
    )
    assert row.is_below_minimum is False
    assert row.below_minimum_ack_by_user_id is None, (
        "An acknowledgement on a compliant rate would read later as though somebody "
        "had waved something through."
    )


def test_the_minimum_is_checked_on_the_derived_hourly_rate(employee, pay_group, minimum_wage):
    """A monthly salary that looks generous can still be under the floor.

    R4,500 a month over a 45-hour week is about R23 an hour against a R30.23 floor.
    Comparing monthly figures instead would need the gazette's monthly column, which
    is stored as published and rounds.
    """
    with pytest.raises(BelowMinimumWageError) as caught:
        capture(employee, pay_basis="monthly", rate_amount=Decimal("4500"), effective_from=START)
    assert caught.value.offered < NMW_HOURLY


def test_the_check_refuses_when_no_minimum_wage_is_loaded(employee, pay_group):
    with pytest.raises(RemunerationRefusedError) as caught:
        capture(employee, pay_basis="monthly", rate_amount=Decimal("9000"), effective_from=START)
    assert "loadstatutory" in str(caught.value)


def test_a_band_with_no_rows_falls_back_to_the_all_row(employee, pay_group, minimum_wage):
    """D-105: the band is passed in, and the fallback order resolves it.

    No loaded rate uses a band today — all four minimum wage rows are `all`, domestic
    workers having been brought to National Minimum Wage parity. So asking for a band
    that has no rows must resolve rather than raise, and choosing a band from an
    employee's hours is deliberately NOT done here: the 27-hour split is a gazetted
    threshold, the no-hard-coded-rate guard caught it written as a literal, and
    nobody has confirmed it is still live.
    """
    check = check_minimum_wage(
        employee,
        hourly_rate=Decimal("35"),
        on_date=START,
        hours_band=MinimumWageRate.HoursBand.LTE_27,
    )
    assert check.rule == minimum_wage
    assert check.clears


# ----------------------------------------------------- superseding a rate


def test_an_increase_closes_the_previous_rate_on_its_start_date(employee, pay_group, minimum_wage):
    """``effective_to`` is EXCLUSIVE, so the old row ends ON the new row's start date.

    A day earlier leaves 30 June with no rate at all, which is silent; a day later
    overlaps and the exclusion constraint refuses, which is the lucky outcome.
    """
    capture(employee, pay_basis="monthly", rate_amount=Decimal("6000"), effective_from=START)
    july = datetime.date(2026, 7, 1)
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6500"),
        effective_from=july,
        change_reason=EmployeeRemuneration.ChangeReason.ANNUAL_INCREASE,
    )

    with tenant_context(employee.tenant_id):
        first, second = EmployeeRemuneration.objects.order_by("effective_from")
        assert first.effective_to == july
        assert second.effective_to is None

        assert rate_in_force(employee, datetime.date(2026, 6, 30)).pk == first.pk
        assert rate_in_force(employee, july).pk == second.pk


def test_two_rates_cannot_be_in_force_at_once(employee, pay_group, minimum_wage):
    """The exclusion constraint, and what the unique alone would have missed.

    A back-dated increase starts INSIDE the open period. The unique on
    (employee, effective_from) sees nothing wrong with it, and two rates in force
    means two answers to "what is this employee paid", chosen by row order.
    """
    capture(employee, pay_basis="monthly", rate_amount=Decimal("6000"), effective_from=START)

    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        EmployeeRemuneration.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement=employee.engagements.first(),
            pay_group=pay_group,
            pay_basis="monthly",
            rate_amount=Decimal("7000"),
            derived_hourly_rate=Decimal("35"),
            derived_daily_rate=Decimal("315"),
            derived_monthly_rate=Decimal("7000"),
            effective_from=datetime.date(2026, 5, 1),
        )


def test_a_historic_rate_still_resolves_after_an_increase(employee, pay_group, minimum_wage):
    """Invariant 2. March must still be March when it is re-run in 2029."""
    capture(employee, pay_basis="monthly", rate_amount=Decimal("6000"), effective_from=START)
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6500"),
        effective_from=datetime.date(2026, 7, 1),
    )

    with tenant_context(employee.tenant_id):
        march = rate_in_force(employee, datetime.date(2026, 3, 15))
    assert march.derived_monthly_rate == Decimal("6000.000000")


# ------------------------------------------------------------------- the cache


def test_capturing_a_rate_fills_the_cache_on_the_employee(employee, pay_group, minimum_wage):
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        as_at=START,
    )

    with tenant_context(employee.tenant_id):
        employee.refresh_from_db()
    assert employee.current_pay_group_id == pay_group.pk
    assert employee.current_pay_basis == "monthly"


def test_a_future_dated_increase_does_not_move_the_cache_yet(employee, pay_group, minimum_wage):
    """THE HALF THE NIGHTLY JOB EXISTS FOR (D-18).

    A rate captured in March to start in July must leave the cache showing March's
    pay group. Maintaining the cache on write ALONE would move it the moment the row
    was saved, and the employee list would group by a pay group that does not apply
    for another four months.
    """
    with tenant_context(employee.tenant_id):
        weekly = PayGroup.objects.create(
            tenant=employee.tenant,
            employer=employee.employer,
            name="Weekly cleaners",
            pay_frequency=PayGroup.PayFrequency.WEEKLY,
            period_end_rule=PayGroup.PeriodEndRule.WEEK_ENDING_DAY,
            week_ending_weekday=4,
            first_period_start=START,
        )

    # as_at is "the day this capture happened", which is March — not the July date
    # the new rate starts on.
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        as_at=START,
    )
    capture(
        employee,
        pay_basis="weekly",
        rate_amount=Decimal("1600"),
        effective_from=datetime.date(2026, 7, 1),
        pay_group=weekly,
        as_at=datetime.date(2026, 3, 15),
    )

    with tenant_context(employee.tenant_id):
        employee.refresh_from_db()
        assert employee.current_pay_basis == "monthly", "July's rate must not be showing in March."

        # What the nightly job does on 1 July, and nobody touches the record.
        assert refresh_pay_cache(employee, on_date=datetime.date(2026, 7, 1)) is True
        employee.refresh_from_db()
        assert employee.current_pay_basis == "weekly"
        assert employee.current_pay_group_id == weekly.pk


def test_refreshing_an_unchanged_cache_reports_that_nothing_moved(
    employee, pay_group, minimum_wage
):
    """So the nightly job can report a count rather than the word 'done'."""
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        as_at=START,
    )
    with tenant_context(employee.tenant_id):
        employee.refresh_from_db()
        assert refresh_pay_cache(employee, on_date=START) is False
