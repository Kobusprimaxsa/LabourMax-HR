"""The capture grid — pre-fill for salaried bases only, bulk fill leaves
captured days untouched.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.capture import capture
from attendance.grid import bulk_fill, month_grid
from attendance.models import AttendanceDay
from calculators.attendance import DayType
from core.managers import tenant_context
from core.models import Tenant
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from employers.models import Employer, PayGroup
from statutory.models import Sector, WorkingTimeRuleSet

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
MONDAY = datetime.date(2026, 3, 2)
TUESDAY = datetime.date(2026, 3, 3)
WEDNESDAY = datetime.date(2026, 3, 4)


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
def monthly_pay_group(db, tenant, employer):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly staff",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=START,
        )


@pytest.fixture
def hourly_pay_group(db, tenant, employer):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Hourly staff",
            pay_frequency=PayGroup.PayFrequency.HOURLY,
            first_period_start=START,
        )


def make_employee(tenant, employer, pay_group, *, id_number, first_name):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name=first_name,
            last_name="Test",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email=f"{first_name.lower()}@example.com",
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
                start_time=datetime.time(8, 0) if cycle_day < 5 else None,
                end_time=datetime.time(17, 0) if cycle_day < 5 else None,
                unpaid_break_minutes=60,
            )
        return person


@pytest.fixture
def monthly_employee(db, tenant, employer, monthly_pay_group):
    return make_employee(
        tenant, employer, monthly_pay_group, id_number="9001015009086", first_name="Monthly"
    )


@pytest.fixture
def hourly_employee(db, tenant, employer, hourly_pay_group):
    return make_employee(
        tenant, employer, hourly_pay_group, id_number="9002026009083", first_name="Hourly"
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
        accommodation_deduction_capped=True,
        accommodation_deduction_max_pct=Decimal("10"),
    )


def test_a_monthly_employees_grid_prefills(monthly_employee, rules):
    grid = month_grid([monthly_employee], START)

    row = grid.rows[0]
    monday_cell = next(c for c in row.cells if c.work_date == MONDAY)

    assert monday_cell.is_captured is False
    assert monday_cell.prefill is not None
    assert monday_cell.prefill.day_type == DayType.ORDINARY
    assert monday_cell.prefill.time_in == datetime.time(8, 0)


def test_an_hourly_employees_grid_does_not_prefill(hourly_employee, rules):
    grid = month_grid([hourly_employee], START)

    row = grid.rows[0]
    monday_cell = next(c for c in row.cells if c.work_date == MONDAY)

    assert monday_cell.is_captured is False
    assert monday_cell.prefill is None


def test_a_captured_day_shows_no_prefill(monthly_employee, rules):
    capture(
        monthly_employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    grid = month_grid([monthly_employee], START)
    row = grid.rows[0]
    monday_cell = next(c for c in row.cells if c.work_date == MONDAY)

    assert monday_cell.is_captured is True
    assert monday_cell.prefill is None


def test_a_non_working_day_has_no_prefill(monthly_employee, rules):
    saturday = datetime.date(2026, 3, 7)
    grid = month_grid([monthly_employee], START)
    row = grid.rows[0]
    saturday_cell = next(c for c in row.cells if c.work_date == saturday)

    assert saturday_cell.prefill is None


def test_bulk_fill_leaves_a_captured_day_untouched(monthly_employee, tenant, rules):
    captured = capture(
        monthly_employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
        comment="already here",
    )

    bulk_fill(
        monthly_employee,
        [MONDAY, TUESDAY, WEDNESDAY],
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    with tenant_context(tenant.pk):
        monday_after = AttendanceDay.objects.get(employee=monthly_employee, work_date=MONDAY)
        tuesday_after = AttendanceDay.objects.get(employee=monthly_employee, work_date=TUESDAY)

    assert monday_after.pk == captured.pk
    assert monday_after.comment == "already here"
    assert tuesday_after.comment == ""
    assert tuesday_after.ordinary_hours == Decimal("8.000")


def test_bulk_fill_only_creates_the_empty_dates(monthly_employee, tenant, rules):
    capture(
        monthly_employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    created = bulk_fill(
        monthly_employee,
        [MONDAY, TUESDAY, WEDNESDAY],
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    assert {day.work_date for day in created} == {TUESDAY, WEDNESDAY}
