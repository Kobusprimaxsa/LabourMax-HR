"""A small world for view tests: two tenants, each fully set up, and a signed-in
client for either. Shared by the isolation suite and every screen's own tests,
so a new screen's tests start from the same two tenants the boundary is proved
against.
"""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal
from types import SimpleNamespace

from django.db import transaction
from django.test import Client

from core.managers import tenant_context
from core.models import AppUser, Tenant, TenantMembership
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeRemuneration, WorkSchedule, WorkScheduleDay
from employers.models import Employer, PayGroup
from statutory.models import Sector, StatutoryParameter, WorkingTimeRuleSet

JUNE = datetime.date(2026, 6, 1)
MONTH = "2026-06"
PASSWORD = "a-long-test-password"
_numbers = itertools.count(100)


def reference_rows():
    """The minimum reference data a capture needs, created once per test."""
    StatutoryParameter.objects.get_or_create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        effective_from=datetime.date(1997, 12, 1),
        defaults={
            "value_numeric": Decimal("15.000000"),
            "unit": StatutoryParameter.Unit.YEARS,
            "source_reference": "BCEA 75 of 1997, s43(1)",
        },
    )
    if not WorkingTimeRuleSet.objects.filter(sector__isnull=True).exists():
        WorkingTimeRuleSet.objects.create(
            sector=None,
            effective_from=datetime.date(1997, 12, 1),
            source_reference="Test fixture (the BCEA default shape)",
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
            night_allowance_type="by_agreement",
            night_allowance_value=None,
            standby_allowance_per_shift=Decimal("0"),
            standby_window_start=datetime.time(18, 0),
            standby_window_end=datetime.time(6, 0),
            standby_hours_before_overtime=Decimal("0"),
            min_paid_hours_per_day=Decimal("4"),
            meal_interval_after_hours=Decimal("5"),
            meal_interval_minutes=60,
            daily_rest_hours=12,
            weekly_rest_hours=36,
            accommodation_deduction_capped=False,
            accommodation_deduction_max_pct=None,
        )
    return Sector.objects.get_or_create(
        code=Sector.Code.DOMESTIC, defaults={"name": "Domestic worker sector"}
    )[0]


def an_employee(world, *, first="Thandi", last=None, schedule=True):
    n = next(_numbers)
    body = f"9001015{n:03d}08"[:12]
    with transaction.atomic(), tenant_context(world.tenant.pk):
        employee = Employee.objects.create(
            tenant=world.tenant,
            employer=world.employer,
            first_name=first,
            last_name=last or f"Mokoena{n}",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number=f"+2782200{n:04d}",
            email=f"worker{n}@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
    engagement = engage(employee, start_date=datetime.date(2026, 1, 5), job_title="Worker")
    with transaction.atomic(), tenant_context(world.tenant.pk):
        EmployeeRemuneration.objects.create(
            tenant=world.tenant,
            employee=employee,
            engagement=engagement,
            pay_group=world.group,
            pay_basis=world.basis,
            rate_amount=Decimal("5000.00") if world.basis == "monthly" else Decimal("30.00"),
            derived_hourly_rate=Decimal("25.641026"),
            derived_daily_rate=Decimal("230.769231"),
            derived_monthly_rate=Decimal("5000.000000"),
            effective_from=datetime.date(2026, 1, 5),
        )
        if schedule:
            made = WorkSchedule.objects.create(
                tenant=world.tenant,
                employee=employee,
                days_per_week=Decimal("5"),
                ordinary_hours_per_week=Decimal("40"),
                effective_from=datetime.date(2026, 1, 5),
            )
            for cycle_day in range(7):
                working = cycle_day < 5
                WorkScheduleDay.objects.create(
                    tenant=world.tenant,
                    work_schedule=made,
                    cycle_day=cycle_day,
                    is_working_day=working,
                    ordinary_hours=Decimal("8") if working else Decimal("0"),
                    start_time=datetime.time(8, 0) if working else None,
                    end_time=datetime.time(17, 0) if working else None,
                    unpaid_break_minutes=60 if working else 0,
                )
    return employee


def a_world(name: str, *, role: str = "owner", basis: str = "monthly", staff: int = 1):
    """One tenant, signed-in-able: an employer, a monthly pay group, and staff."""
    sector = reference_rows()
    tenant = Tenant.objects.create(trading_name=name)
    user = AppUser.objects.create_user(
        email=f"{name.lower()}-{role}@example.com", password=PASSWORD
    )
    with transaction.atomic(), tenant_context(tenant.pk):
        employer = Employer.objects.create(tenant=tenant, trading_name=name, sector=sector)
        group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Hourly" if basis in ("hourly", "daily") else "Monthly",
            pay_frequency=basis if basis in ("hourly", "daily") else "monthly",
            first_period_start=datetime.date(2026, 3, 1),
        )
        membership = TenantMembership.objects.create(tenant=tenant, user=user, role=role)
    world = SimpleNamespace(
        tenant=tenant,
        user=user,
        membership=membership,
        employer=employer,
        group=group,
        basis=basis,
    )
    world.employees = [an_employee(world) for _ in range(staff)]
    world.employee = world.employees[0] if world.employees else None
    return world


def a_member(world, role: str) -> AppUser:
    """Another user in the same tenant, with another role."""
    user = AppUser.objects.create_user(
        email=f"{world.tenant.trading_name.lower()}-{role}-{next(_numbers)}@example.com",
        password=PASSWORD,
    )
    with transaction.atomic(), tenant_context(world.tenant.pk):
        TenantMembership.objects.create(tenant=world.tenant, user=user, role=role)
    return user


def client_for(user, tenant) -> Client:
    client = Client()
    client.force_login(user)
    session = client.session
    session["active_tenant_id"] = tenant.pk
    session.save()
    return client


def grid_kwargs(world, month: str = MONTH) -> dict:
    return {
        "employer_uid": world.employer.public_uid,
        "group_uid": world.group.public_uid,
        "month": month,
    }
