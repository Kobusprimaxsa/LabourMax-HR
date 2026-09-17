"""An accrual method the statute does not offer is refused at capture (D-192).

BCEA s20(2)(b) and (c) — one day per seventeen days worked, one hour per
seventeen hours worked — are ANNUAL leave methods, selected by agreement. Sick
leave is s22(2), the days normally worked in six weeks; s22(3)'s one per 26 days
worked restricts availability in the first six months and is not a method.
Family responsibility leave is s27(2), a flat count of days per annual cycle.
Neither has a method to select, so an entitlement row naming one records an
agreement that cannot exist — and the engine ignoring it (D-190) is the D-134
shape: the stored row says one thing and the engine does another.

Keyed on the SYSTEM rows. ``leave_type`` is shared (D-87) and a tenant may define
its own row with any code (D-127): an employer's own "SICK" or "BIRTHDAY" row
with a per-hours method is its own business and keeps working.

Every refusal is asserted on its message (D-134).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from core.managers import platform_context, tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeLeaveEntitlement
from employers.models import Employer
from leave.models import LeaveType
from leave.types import seed_system_leave_types
from statutory.models import Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
Method = EmployeeLeaveEntitlement.AccrualMethod
NOT_OFFERED = [Method.PER_HOURS_WORKED, Method.PER_DAYS_WORKED, Method.UPFRONT_ANNUAL]


@pytest.fixture
def employee(db):
    StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("15.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s43(1)",
    )
    sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    tenant = Tenant.objects.create(trading_name="Household")
    body = "900101500908"
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
    engage(person, start_date=START, job_title="Domestic worker")
    return person


@pytest.fixture
def system_types(db):
    seed_system_leave_types()
    with platform_context():
        return {t.code: t for t in LeaveType.objects.filter(tenant__isnull=True)}


def _row(employee, leave_type, method):
    return EmployeeLeaveEntitlement(
        tenant=employee.tenant,
        employee=employee,
        leave_type=leave_type,
        accrual_method=method,
        effective_from=START,
    )


# ----------------------------------------------------------------- the refusal


@pytest.mark.parametrize(
    ("code", "citation"),
    [("SICK", "s22(2)"), ("FAMILY_RESPONSIBILITY", "s27(2)")],
)
def test_clean_refuses_a_method_the_statute_does_not_offer(employee, system_types, code, citation):
    with tenant_context(employee.tenant_id):
        row = _row(employee, system_types[code], Method.PER_HOURS_WORKED)
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    message = str(raised.value)
    assert f"{code} leave cannot use the per_hours_worked accrual method" in message, message
    assert "BCEA s20(2)" in message, message
    assert citation in message, message


@pytest.mark.parametrize("code", ["SICK", "FAMILY_RESPONSIBILITY"])
@pytest.mark.parametrize("method", NOT_OFFERED)
def test_the_database_refuses_it_whichever_path_wrote_it(employee, system_types, code, method):
    """No clean(): the bulk importer, a data migration and psql never call it."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with tenant_context(employee.tenant_id):
            _row(employee, system_types[code], method).save()

    message = str(raised.value)
    assert f"{code} leave cannot use the {method} accrual method" in message, message
    assert "BCEA s20(2)" in message, message


def test_the_database_refuses_it_on_update_too(employee, system_types):
    with tenant_context(employee.tenant_id):
        _row(employee, system_types["SICK"], Method.MONTHLY).save()
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with tenant_context(employee.tenant_id):
            EmployeeLeaveEntitlement.objects.filter(employee=employee).update(
                accrual_method=Method.PER_HOURS_WORKED
            )

    assert "SICK leave cannot use the per_hours_worked accrual method" in str(raised.value)


# ------------------------------------------------------------ what stays legal


def test_the_default_method_on_system_sick_leave_is_accepted(employee, system_types):
    """An entitlement row for sick leave is ordinary — extra days, a flat total.
    Only a method the statute does not offer is refused."""
    with tenant_context(employee.tenant_id):
        row = _row(employee, system_types["SICK"], Method.MONTHLY)
        row.additional_days_per_cycle = Decimal("5.000")
        row.full_clean()
        row.save()


def test_system_annual_leave_may_use_per_hours_worked(employee, system_types):
    with tenant_context(employee.tenant_id):
        row = _row(employee, system_types["ANNUAL"], Method.PER_HOURS_WORKED)
        row.full_clean()
        row.save()


def test_a_tenants_own_row_coded_sick_is_not_the_statutory_one(employee, system_types):
    """D-127: two tenants may each define the same code. A refusal keyed on the
    string 'SICK' without is_system would refuse this employer's own row."""
    with tenant_context(employee.tenant_id):
        own = LeaveType.objects.create(tenant=employee.tenant, code="SICK", name="Our sick scheme")
        row = _row(employee, own, Method.PER_HOURS_WORKED)
        row.full_clean()
        row.save()


def test_an_employers_own_hourly_leave_keeps_its_hours(employee, system_types):
    """Birthday leave on one hour per seventeen worked is the employer's own
    scheme. It is captured, AND its unit stays hours — D-190's fix ignored the
    method for every non-ANNUAL code, which silently turned this into days."""
    from leave.cycles import accrual_method_for, unit_for_method
    from leave.models import LeaveCycle

    with tenant_context(employee.tenant_id):
        birthday = LeaveType.objects.create(
            tenant=employee.tenant, code="BIRTHDAY", name="Birthday leave"
        )
        row = _row(employee, birthday, Method.PER_HOURS_WORKED)
        row.full_clean()
        row.save()
        method, _entitlement = accrual_method_for(employee, birthday, START)

    assert method == Method.PER_HOURS_WORKED
    assert unit_for_method(method) == LeaveCycle.Unit.HOURS


# ------------------------------------------------- the pre-check for old rows


def test_the_pre_check_finds_an_old_row_with_no_tenant_pinned(employee, system_types):
    """A migration runs with no tenant pinned, and FORCE RLS returns no rows to
    such a session — a plain count would report zero violations whatever
    exists. The pre-check must not be blind: a row written before the trigger
    (simulated by disabling it) is found, named, and refused."""
    from employees.statutory_methods import (
        TRIGGER_NAME,
        refuse_existing_violations,
        statutory_method_violations,
    )

    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE employee_leave_entitlement DISABLE TRIGGER {TRIGGER_NAME}")
    with tenant_context(employee.tenant_id):
        _row(employee, system_types["SICK"], Method.PER_HOURS_WORKED).save()
    with connection.cursor() as cursor:
        # The insert queued deferred FK checks; ALTER TABLE refuses while they pend.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE employee_leave_entitlement ENABLE TRIGGER {TRIGGER_NAME}")

    found = statutory_method_violations(connection)
    assert len(found) == 1
    assert found[0]["leave_type_code"] == "SICK"
    assert found[0]["accrual_method"] == "per_hours_worked"

    with pytest.raises(RuntimeError) as raised:
        refuse_existing_violations(connection)
    message = str(raised.value)
    assert "1 employee_leave_entitlement row" in message, message
    assert f"employee {employee.pk}" in message, message
    assert "SICK / per_hours_worked" in message, message
