"""Shared fixtures for the leave/ test suite (P6 chunk 1).

One BCEA-default ``LeaveRuleSet`` (``sector=None``) so every test reads the
same figures regardless of which sector the fixture employer sits in —
``statutory.resolve._rule_set`` falls back to the NULL-sector row, which is
exactly the row this fixture creates. The numbers below are plausible test
data, not a statutory citation someone should build a payslip against — the
same status as ``WorkingTimeRuleSet``'s own test fixture in
``attendance/tests/test_summary.py``.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import platform_context, tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, WorkSchedule
from employers.models import Employer
from leave.models import LeaveType
from leave.types import seed_system_leave_types
from statutory.models import LeaveRuleSet, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)


def make_id(sequence="5009"):
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def minimum_age(db):
    return StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("15.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s43(1)",
    )


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


@pytest.fixture
def employee(db, tenant, employer):
    with tenant_context(tenant.pk):
        return Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=BORN,
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=make_id(),
        )


@pytest.fixture
def engagement(employee, minimum_age):
    """Engages the fixture employee from START. The current engagement."""
    return engage(employee, start_date=START, job_title="Domestic worker")


@pytest.fixture
def leave_rules(db):
    """The BCEA default (``sector=None``) leave rule set every test resolves
    against, whatever sector the fixture employer happens to carry."""
    return LeaveRuleSet.objects.create(
        sector=None,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Test fixture",
        annual_leave_days_per_cycle_5day=Decimal("15"),
        annual_leave_days_per_cycle_6day=Decimal("18"),
        annual_accrual_days_per_month_5day=Decimal("1.25"),
        annual_accrual_days_per_month_6day=Decimal("1.5"),
        annual_accrual_ratio_days_worked=17,
        annual_accrual_ratio_hours_worked=17,
        annual_leave_cycle_months=12,
        annual_leave_forfeit_months=6,
        annual_leave_payable_on_termination=True,
        sick_leave_cycle_months=36,
        sick_leave_weeks_equivalent=Decimal("6"),
        sick_leave_first_six_months_ratio=26,
        sick_leave_payable_on_termination=False,
        family_responsibility_days=3,
        family_resp_min_service_months=4,
        family_resp_min_days_per_week=4,
        parental_leave_total_months=4,
        parental_leave_additional_days=10,
        parental_leave_shareable=True,
        maternity_earliest_start_weeks_before_birth=4,
        maternity_no_work_weeks_after_birth=6,
    )


@pytest.fixture
def annual_type(db):
    """The seeded, shared ANNUAL leave type — task 1's own catalogue, read
    rather than re-invented so this suite exercises the real seed."""
    seed_system_leave_types()
    with platform_context():
        return LeaveType.objects.get(code=LeaveType.Code.ANNUAL, tenant__isnull=True)


@pytest.fixture
def schedule_5day(tenant, employee):
    with tenant_context(tenant.pk):
        return WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )


@pytest.fixture
def schedule_6day(tenant, employee):
    with tenant_context(tenant.pk):
        return WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal("6"),
            ordinary_hours_per_week=Decimal("48"),
            effective_from=START,
        )
