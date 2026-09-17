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
from statutory.models import Sector, StatutoryParameter, WorkingTimeRuleSet

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


def _working_time_rules(*, sector, capped, ceiling=None):
    """A working time rule set carrying the accommodation cap under test.
    Every other figure is a placeholder this module never reads."""
    return WorkingTimeRuleSet.objects.create(
        sector=sector,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Test fixture: Sectoral Determination 7",
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
        accommodation_deduction_capped=capped,
        accommodation_deduction_max_pct=None if ceiling is None else Decimal(ceiling),
    )


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
    _working_time_rules(sector=employee.employer.sector, capped=True, ceiling="10.00")
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


# ------------------------------------------------ the gazetted ceiling (D-198)


def _clean_line(employee, percentage):
    accom = _system_accom(Method.PERCENTAGE_OF_BASE)
    with tenant_context(employee.tenant_id):
        row = _line(employee, accom, percentage_of_basic=Decimal(percentage))
        row.full_clean()
        row.save()
    return row


def test_a_percentage_above_the_gazetted_ceiling_is_refused_naming_both_figures(employee):
    """SD7 caps the accommodation deduction at 10 percent of the wage. A line above
    it is an unlawful deduction on every payslip it touches, so capture refuses."""
    _working_time_rules(sector=employee.employer.sector, capped=True, ceiling="10.00")

    with pytest.raises(ValidationError) as raised:
        _clean_line(employee, "10.0100")

    message = str(raised.value)
    assert "10.01%" in message, message
    assert "ceiling of 10.00%" in message, message
    assert "working_time_rule_set.accommodation_deduction_max_pct" in message, message
    assert "Test fixture: Sectoral Determination 7" in message, message
    with tenant_context(employee.tenant_id):
        assert not EmployeeRecurringComponent.objects.filter(employee=employee).exists()


def test_a_percentage_at_the_ceiling_is_accepted(employee):
    _working_time_rules(sector=employee.employer.sector, capped=True, ceiling="10.00")
    assert _clean_line(employee, "10.0000").pk is not None


def test_no_cap_set_allows_any_percentage(employee):
    """NO_CAP: the instrument states no accommodation percentage — the BCEA
    default and SD1 both. Reached here through the BCEA fallback, no row for the
    employer's own sector. The absence of a cap is now a boolean, not a 0.00
    standing in for it (D-198 amended)."""
    _working_time_rules(sector=None, capped=False)
    assert _clean_line(employee, "25.0000").pk is not None


def test_a_cap_of_zero_refuses_any_accommodation_deduction(employee):
    """CAPPED(0): an instrument that forbids the deduction outright. Under the
    0.00 sentinel this case was UNREACHABLE — zero read as unlimited, the
    maximally wrong answer — so it is written first and watched fail. Nothing
    loaded uses it today; it is representable, which is the point."""
    _working_time_rules(sector=employee.employer.sector, capped=True, ceiling="0.00")

    with pytest.raises(ValidationError) as raised:
        _clean_line(employee, "0.5000")

    message = str(raised.value)
    assert "0.50% exceeds the ceiling of 0.00%" in message, message
    with tenant_context(employee.tenant_id):
        assert not EmployeeRecurringComponent.objects.filter(employee=employee).exists()


def test_the_two_columns_must_agree(db):
    """The paired CHECKs: a cap with no percentage, or a percentage with no cap,
    is a half-written row — and a row written without thinking about
    accommodation at all fails outright, because the boolean has no default."""
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _working_time_rules(sector=None, capped=True, ceiling=None)
    assert "working_time_capped_states_its_percentage" in str(raised.value)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        _working_time_rules(sector=None, capped=False, ceiling="10.00")
    assert "working_time_uncapped_states_no_percentage" in str(raised.value)


def test_no_rule_set_loaded_refuses_rather_than_skipping_the_check(employee):
    """D-101's shape: there is no staleness guard behind a capture-time check, so a
    missing figure refuses instead of letting an unchecked percentage through."""
    with pytest.raises(ValidationError) as raised:
        _clean_line(employee, "5.0000")

    message = str(raised.value)
    assert "accommodation deduction ceiling cannot be checked" in message, message
    assert "No working time rule set" in message, message
