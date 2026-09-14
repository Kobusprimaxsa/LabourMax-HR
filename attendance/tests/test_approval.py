"""Approval — refused while a blocking exception stands, naming it. A warning
does not block.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.approval import ApprovalRefusedError, approve
from attendance.capture import capture
from attendance.models import AttendanceDay
from calculators.attendance import DayType
from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from employers.models import Employer
from statutory.models import Sector, WorkingTimeRuleSet

pytestmark = pytest.mark.django_db

MONDAY = datetime.date(2026, 3, 2)


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
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number="9001015009086",
        )


@pytest.fixture
def approver(db):
    return AppUser.objects.create_user(email="owner@example.com", password="x" * 16)


@pytest.fixture
def schedule(db, tenant, employee):
    with tenant_context(tenant.pk):
        made = WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=datetime.date(2026, 3, 1),
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=made,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
        return made


@pytest.fixture
def rules(db):
    return WorkingTimeRuleSet.objects.create(
        sector=None,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Test fixture",
        ordinary_hours_per_week=Decimal("45"),
        ordinary_hours_per_day_5day=Decimal("9"),
        ordinary_hours_per_day_6day=Decimal("8"),
        overtime_multiplier=Decimal("1.5"),
        max_overtime_hours_per_day=Decimal("3"),
        max_overtime_hours_per_week=Decimal("10"),
        sunday_multiplier_ordinary=Decimal("1.5"),
        sunday_multiplier_non_ordinary=Decimal("2.0"),
        public_holiday_worked_multiplier=Decimal("2.0"),
        public_holiday_not_worked_paid=True,
        night_work_start_time=datetime.time(18, 0),
        night_work_end_time=datetime.time(6, 0),
        night_allowance_type="percentage",
        night_allowance_value=Decimal("10"),
        standby_allowance_per_shift=Decimal("50.00"),
        standby_window_start=datetime.time(18, 0),
        standby_window_end=datetime.time(6, 0),
        standby_hours_before_overtime=Decimal("2"),
        min_paid_hours_per_day=Decimal("6"),
        meal_interval_after_hours=Decimal("5"),
        meal_interval_minutes=60,
        daily_rest_hours=12,
        weekly_rest_hours=36,
        accommodation_deduction_max_pct=Decimal("10"),
    )


def test_approval_moves_captured_to_approved(employee, tenant, rules, approver, schedule):
    day = capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    approve([day], approver)

    with tenant_context(tenant.pk):
        day.refresh_from_db()
    assert day.status == AttendanceDay.Status.APPROVED
    assert day.updated_by_user_id == approver.pk


def test_approval_refuses_while_a_blocking_exception_stands_and_names_it(
    employee, tenant, rules, approver
):
    day = capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(0, 0),
        time_out=datetime.time(15, 0),
        unpaid_break_minutes=0,
    )  # 15 hours worked: 9 ordinary + 6 overtime, over the 3-hour daily maximum

    with pytest.raises(ApprovalRefusedError) as raised:
        approve([day], approver)

    assert "max_overtime_hours_per_day" in str(raised.value)
    assert str(MONDAY) in str(raised.value)

    with tenant_context(tenant.pk):
        day.refresh_from_db()
    assert day.status == AttendanceDay.Status.CAPTURED


def test_a_warning_does_not_block_approval(employee, tenant, rules, approver, schedule):
    """A long shift with no proper meal break is a WARNING exception (chunk
    1's meal-interval check) and must not stop approval.
    """
    day = capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(7, 0),
        time_out=datetime.time(15, 0),
        unpaid_break_minutes=15,  # under the 60-minute meal_interval_minutes
    )

    approve([day], approver)

    with tenant_context(tenant.pk):
        day.refresh_from_db()
    assert day.status == AttendanceDay.Status.APPROVED
