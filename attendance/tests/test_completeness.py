"""The P7 hook — missing scheduled days, attendance-driven bases only."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.completeness import missing_attendance_days
from core.managers import tenant_context
from core.models import Tenant
from employees.models import Employee, WorkSchedule, WorkScheduleDay
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
        person.current_pay_group = pay_group
        person.save(update_fields=["current_pay_group", "updated_at"])
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
