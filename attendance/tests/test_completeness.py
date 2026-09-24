"""The P7 hook — missing scheduled days, attendance-driven bases only."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.completeness import missing_attendance_days
from core.managers import tenant_context
from core.models import Tenant
from employees.models import (
    Employee,
    EmployeeEngagement,
    EmployeeRemuneration,
    WorkSchedule,
    WorkScheduleDay,
)
from employers.models import Employer, PayGroup
from statutory.models import Sector

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
END = datetime.date(2026, 3, 31)


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Household")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)


class Period:
    """Anything with period_start/period_end - a PayPeriod stand-in."""

    def __init__(self, period_start, period_end):
        self.period_start = period_start
        self.period_end = period_end


def make_employee(tenant, employer, pay_group, id_number):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Test",
            last_name="Employee",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email=f"test{id_number}@example.com",
            id_number=id_number,
        )
        # The ROWS, not the cache (D-314): an engagement and the remuneration in
        # force on the pay group. current_pay_group is deliberately left unset.
        engagement = EmployeeEngagement.objects.create(
            tenant=tenant, employee=person, start_date=START
        )
        EmployeeRemuneration.objects.create(
            tenant=tenant,
            employee=person,
            engagement=engagement,
            pay_group=pay_group,
            pay_basis="hourly" if pay_group.is_attendance_driven else "monthly",
            rate_amount=Decimal("30.00"),
            derived_hourly_rate=Decimal("30.000000"),
            derived_daily_rate=Decimal("240.000000"),
            derived_monthly_rate=Decimal("5200.000000"),
            effective_from=START,
        )
        schedule = WorkSchedule.objects.create(
            tenant=tenant,
            employee=person,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=schedule,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
        return person


@pytest.fixture
def hourly_employee(db, tenant, employer):
    with tenant_context(tenant.pk):
        pay_group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Hourly staff",
            pay_frequency=PayGroup.PayFrequency.HOURLY,
            first_period_start=START,
        )
    return make_employee(tenant, employer, pay_group, "9001015009086")


@pytest.fixture
def monthly_employee(db, tenant, employer):
    with tenant_context(tenant.pk):
        pay_group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly staff",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=START,
        )
    return make_employee(tenant, employer, pay_group, "9002026009083")


def test_missing_days_are_returned_for_an_hourly_employee(hourly_employee):
    missing = missing_attendance_days(hourly_employee, Period(START, END))

    # Every Monday-Friday in March 2026 with no attendance_day row at all.
    assert len(missing) > 0
    assert all(d.weekday() < 5 for d in missing)
    assert datetime.date(2026, 3, 2) in missing  # a Monday


def test_nothing_is_missing_for_a_monthly_employee(monthly_employee):
    missing = missing_attendance_days(monthly_employee, Period(START, END))

    assert missing == []


def test_the_check_reads_the_rows_not_todays_cache(hourly_employee):
    """D-314, PROVE EVERY GUARD FAILS: the cache says nothing about a pay group
    (it is refreshed as at TODAY, D-107, and here was never set) — the check
    used to read it and return nothing at all. The remuneration row says
    hourly, and that is what is read."""
    assert hourly_employee.current_pay_group_id is None
    assert len(missing_attendance_days(hourly_employee, Period(START, END))) == 22


def test_a_leavers_days_after_the_last_one_are_not_missing(hourly_employee):
    """D-314: a leaver whose last day was 13 March owes no attendance for the
    rest of the month. 2 to 13 March holds ten weekdays; nothing after counts."""
    with tenant_context(hourly_employee.tenant_id):
        EmployeeEngagement.objects.filter(employee=hourly_employee).update(
            termination_date=datetime.date(2026, 3, 13), termination_reason_code="resignation"
        )
    missing = missing_attendance_days(hourly_employee, Period(START, END))
    assert len(missing) == 10
    assert max(missing) == datetime.date(2026, 3, 13)
