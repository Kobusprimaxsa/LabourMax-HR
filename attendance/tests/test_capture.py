"""Capturing a day — buckets computed from the rule set in force, stored, and
frozen the moment a payroll run locks the day.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import DatabaseError, IntegrityError, transaction

from attendance.capture import LockedDayError, capture
from attendance.models import AttendanceDay
from calculators.attendance import DayType
from core.managers import tenant_context
from core.models import Tenant
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from employers.models import Employer
from statutory.models import Sector, WorkingTimeRuleSet

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
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


@pytest.fixture
def schedule(db, tenant, employee):
    with tenant_context(tenant.pk):
        made = WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=made,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,  # Monday-Friday
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
        return made


def test_capture_buckets_and_stores_an_ordinary_day(employee, rules, schedule):
    day = capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    assert day.ordinary_hours == Decimal("8.000")
    assert day.overtime_hours == Decimal("0.000")
    assert day.status == AttendanceDay.Status.CAPTURED


def test_capture_updates_an_existing_day_rather_than_duplicating_it(employee, rules, schedule):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    updated = capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(18, 0),
        unpaid_break_minutes=60,
    )

    with tenant_context(employee.tenant_id):
        assert AttendanceDay.objects.filter(employee=employee, work_date=MONDAY).count() == 1
    assert updated.overtime_hours == Decimal("1.000")


def test_capture_refuses_when_no_rule_set_is_loaded(employee, schedule):
    from attendance.capture import AttendanceCaptureRefusedError

    with pytest.raises(AttendanceCaptureRefusedError) as raised:
        capture(employee, work_date=MONDAY, day_type=DayType.ORDINARY)

    assert "loadstatutory" in str(raised.value)


def test_capture_refuses_to_write_a_locked_day_naming_the_run(employee, rules, schedule):
    with tenant_context(employee.tenant_id):
        AttendanceDay.objects.create(
            tenant=employee.tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.ORDINARY,
            status=AttendanceDay.Status.LOCKED,
            locked_by_payroll_run_id_ref=42,
        )

    with pytest.raises(LockedDayError) as raised:
        capture(employee, work_date=MONDAY, day_type=DayType.ORDINARY, time_in=datetime.time(8, 0))

    assert "42" in str(raised.value)
    assert "locked" in str(raised.value).lower()


def test_the_trigger_refuses_an_update_to_a_locked_day(employee, rules):
    """The backstop for every path that does not go through capture() — direct
    SQL, a shell, a future bulk-update.
    """
    with tenant_context(employee.tenant_id):
        day = AttendanceDay.objects.create(
            tenant=employee.tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.ORDINARY,
            status=AttendanceDay.Status.LOCKED,
        )

        day.comment = "trying to sneak a change in"
        with pytest.raises(DatabaseError) as raised, transaction.atomic():
            day.save()

    assert "locked by a finalised payroll run" in str(raised.value)


def test_a_non_locked_day_can_still_be_updated_directly(employee, rules):
    with tenant_context(employee.tenant_id):
        day = AttendanceDay.objects.create(
            tenant=employee.tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.ORDINARY,
            status=AttendanceDay.Status.APPROVED,
        )
        day.comment = "fine to edit"
        day.save()
        day.refresh_from_db()

    assert day.comment == "fine to edit"


def test_the_check_refuses_a_leave_day_with_no_leave_application(employee):
    with (
        tenant_context(employee.tenant_id),
        pytest.raises(IntegrityError) as raised,
        transaction.atomic(),
    ):
        AttendanceDay.objects.create(
            tenant=employee.tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.LEAVE,
        )

    assert "attendance_day_leave_needs_a_leave_application" in str(raised.value)


def test_the_check_refuses_a_non_leave_day_that_names_a_leave_application(employee):
    with (
        tenant_context(employee.tenant_id),
        pytest.raises(IntegrityError) as raised,
        transaction.atomic(),
    ):
        AttendanceDay.objects.create(
            tenant=employee.tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.ORDINARY,
            leave_application_id_ref=1,
        )

    assert "attendance_day_leave_needs_a_leave_application" in str(raised.value)


def test_a_leave_day_with_a_leave_application_reference_is_accepted(employee):
    with tenant_context(employee.tenant_id):
        day = AttendanceDay.objects.create(
            tenant=employee.tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.LEAVE,
            leave_application_id_ref=1,
        )

    assert day.leave_application_id_ref == 1
