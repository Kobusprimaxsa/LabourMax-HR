"""The accommodation deduction is a PERCENTAGE of the wage (D-197).

Kobus's decision: ACCOM_DED is captured as a percentage of salary, which payroll
turns into a rand figure. It was seeded FIXED, which — with D-191's rule that a
FIXED component takes no percentage — meant it could only be entered as a rand
amount. The catalogue row becomes ``percentage_of_base``, and a line on a
percentage component must state a percentage, not an amount.

``seedcomponents`` never updates an existing row, so an environment that already
seeded ACCOM_DED is changed by a migration, through the reference-maintenance
flag (documents/0002's precedent) — and that migration REFUSES, naming them, if
any recurring line already captured ACCOM_DED as a rand amount, rather than
rewriting captured data.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from core.managers import platform_context, tenant_context
from core.models import FileObject, Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeRecurringComponent
from employers.components import SYSTEM_COMPONENTS
from employers.models import Employer, PayrollComponent
from statutory.models import Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
Method = PayrollComponent.CalculationMethod


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
            id_number=body + str(luhn_check_digit(body)),
        )
    engage(person, start_date=START, job_title="Domestic worker")
    return person


def _system_accom(method):
    """ACCOM_DED as the platform stocks it, with whichever method is under test."""
    with platform_context():
        return PayrollComponent.objects.create(
            code="ACCOM_DED",
            name="Accommodation deduction",
            component_type=PayrollComponent.ComponentType.DEDUCTION,
            calculation_method=method,
            is_system=True,
            is_taxable=False,
            is_uif_base=False,
            is_sdl_base=False,
            is_coida_base=False,
        )


def _consent(tenant):
    with tenant_context(tenant.pk):
        return FileObject.objects.create(
            tenant=tenant,
            storage_key="consent/accommodation",
            original_filename="accommodation-consent.pdf",
            content_type="application/pdf",
            size_bytes=10,
            checksum_sha256="0" * 64,
            scan_status=FileObject.ScanStatus.CLEAN,
        )


def _line(employee, component, **figures):
    return EmployeeRecurringComponent(
        tenant=employee.tenant,
        employee=employee,
        payroll_component=component,
        effective_from=START,
        written_consent_file=_consent(employee.tenant),
        **figures,
    )


def test_the_catalogue_seeds_accom_ded_as_a_percentage_of_a_base():
    spec = next(c for c in SYSTEM_COMPONENTS if c.code == "ACCOM_DED")
    assert spec.calculation_method == Method.PERCENTAGE_OF_BASE


def test_an_accommodation_deduction_is_captured_as_a_percentage(employee):
    accom = _system_accom(Method.PERCENTAGE_OF_BASE)
    with tenant_context(employee.tenant_id):
        row = _line(employee, accom, percentage_of_basic=Decimal("10.0000"))
        row.full_clean()
        row.save()


def test_a_rand_amount_on_the_accommodation_deduction_is_refused(employee):
    accom = _system_accom(Method.PERCENTAGE_OF_BASE)
    with tenant_context(employee.tenant_id):
        row = _line(employee, accom, amount=Decimal("450.0000"))
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    message = str(raised.value)
    assert "ACCOM_DED is a percentage of a base" in message, message


def test_the_migration_step_turns_a_fixed_accom_ded_into_a_percentage(employee):
    """An environment seeded before D-197: the system row is FIXED, the trigger
    locks it, and the migration step changes it anyway through the maintenance
    flag — and only it."""
    from employers.catalogue_maintenance import accommodation_deduction_as_percentage

    accom = _system_accom(Method.FIXED)
    accommodation_deduction_as_percentage(connection)

    with platform_context():
        accom.refresh_from_db()
    assert accom.calculation_method == Method.PERCENTAGE_OF_BASE


def test_the_migration_step_refuses_rather_than_rewriting_a_captured_rand_amount(employee):
    from employers.catalogue_maintenance import (
        AccommodationDeductionMigrationError,
        accommodation_deduction_as_percentage,
    )

    accom = _system_accom(Method.FIXED)
    with tenant_context(employee.tenant_id):
        row = _line(employee, accom, amount=Decimal("450.0000"))
        row.save()

    with pytest.raises(AccommodationDeductionMigrationError) as raised:
        accommodation_deduction_as_percentage(connection)

    message = str(raised.value)
    assert f"employee {employee.pk}" in message, message
    assert "450" in message, message
    with platform_context():
        accom.refresh_from_db()
    assert accom.calculation_method == Method.FIXED, "refused, so nothing changed"
