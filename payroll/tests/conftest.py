"""Shared scaffolding for the payslip and YTD tests.

Building a payslip needs most of the schema in front of it — a tenant, an
employer, an employee with an engagement, a pay group, a tax year and a period —
so it is assembled once here rather than four times across two files.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import transaction

from calculators.base import ENGINE_VERSION
from core.managers import platform_context, tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeRemuneration
from employers.models import Employer, PayGroup, PayrollComponent
from payroll.models import PayPeriod, PayrollRun, Payslip, PayslipLine
from statutory.models import Sector, StatutoryParameter, TaxYear

MARCH = datetime.date(2026, 3, 31)


@pytest.fixture
def tax_year(db) -> TaxYear:
    return TaxYear.objects.create(
        label="2026/2027",
        start_date=datetime.date(2026, 3, 1),
        end_date=datetime.date(2027, 2, 28),
    )


@pytest.fixture
def household(db) -> dict:
    """A tenant, an employer and one engaged employee."""
    StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("15.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="BCEA 75 of 1997, s43(1)",
    )
    sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    tenant = Tenant.objects.create(trading_name="Household")
    body = "900101500908"
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
        pay_group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly staff",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=datetime.date(2026, 3, 1),
        )
    engagement = engage(employee, start_date=datetime.date(2026, 3, 1), job_title="Domestic worker")
    with tenant_context(tenant.pk):
        EmployeeRemuneration.objects.create(
            tenant=tenant,
            employee=employee,
            engagement=engagement,
            pay_group=pay_group,
            pay_basis=EmployeeRemuneration.PayBasis.MONTHLY,
            rate_amount=Decimal("5000.0000"),
            derived_hourly_rate=Decimal("25.641026"),
            derived_daily_rate=Decimal("230.769231"),
            derived_monthly_rate=Decimal("5000.000000"),
            effective_from=datetime.date(2026, 3, 1),
        )
    # The COIDA ceiling, because the year-to-date cache caps its COIDA figure
    # through payroll/coida.py (D-285, D-290). Notice 3910 of 2026, GG 54577.
    StatutoryParameter.objects.create(
        parameter_code="COIDA_ANNUAL_CEILING",
        value_numeric=Decimal("668000.000000"),
        unit=StatutoryParameter.Unit.ZAR,
        effective_from=datetime.date(2026, 3, 1),
        source_reference="Compensation Fund maximum amount of earnings, 2026/2027",
    )
    return {
        "tenant": tenant,
        "employer": employer,
        "employee": employee,
        "pay_group": pay_group,
        "engagement": engagement,
    }


@pytest.fixture
def basic_component(db) -> PayrollComponent:
    """One shared catalogue row, without seeding the whole catalogue: these
    tests are about the payslip, not about which components exist.

    A NULL-tenant row is written inside ``platform_context()``, and the atomic
    block comes FIRST (D-92): ``set_config(..., true)`` is transaction-local, so
    under autocommit the flag is gone before the insert and RLS refuses it.
    """
    with transaction.atomic(), platform_context():
        return PayrollComponent.objects.create(
            code="BASIC",
            name="Basic wage",
            component_type=PayrollComponent.ComponentType.EARNING,
            calculation_method=PayrollComponent.CalculationMethod.RATE_X_UNITS,
            display_order=10,
            # A shared row IS a system row, by CHECK: without it a NULL-tenant
            # row would be readable by every tenant and deletable by any of them.
            is_system=True,
        )


def a_period(household, tax_year, *, number=1, start=None, payment=None) -> PayPeriod:
    start = start or datetime.date(2026, 3, 1)
    with tenant_context(household["tenant"].pk):
        return PayPeriod.objects.create(
            tenant=household["tenant"],
            pay_group=household["pay_group"],
            tax_year=tax_year,
            period_number=number,
            period_start=start,
            period_end=start + datetime.timedelta(days=27),
            payment_date=payment or (start + datetime.timedelta(days=27)),
            working_days_in_period=Decimal("21.000"),
        )


def a_run(household, period, *, number=1, status=PayrollRun.Status.DRAFT) -> PayrollRun:
    with tenant_context(household["tenant"].pk):
        return PayrollRun.objects.create(
            tenant=household["tenant"],
            employer=household["employer"],
            pay_period=period,
            run_number=number,
            status=status,
            engine_version=ENGINE_VERSION,
        )


def a_payslip(household, run, *, finalised=False, **overrides) -> Payslip:
    values = {
        "tenant": household["tenant"],
        "payroll_run": run,
        "employee": household["employee"],
        "pay_period": run.pay_period,
        "engagement": household["engagement"],
        "payslip_number": f"PS-{run.pk}-{household['employee'].pk}",
        "pay_basis": "monthly",
        "rate_used": Decimal("5000.000000"),
        "gross_remuneration": Decimal("5000.00"),
        "total_earnings": Decimal("5000.00"),
        "total_deductions": Decimal("50.00"),
        "net_pay": Decimal("4950.00"),
    }
    values.update(overrides)
    if finalised:
        values.setdefault("is_finalised", True)
        values.setdefault(
            "finalised_at", datetime.datetime(2026, 3, 28, 10, 0, tzinfo=datetime.UTC)
        )
    with tenant_context(household["tenant"].pk):
        return Payslip.objects.create(**values)


def a_line(household, payslip, component, **overrides) -> PayslipLine:
    exact = overrides.pop("amount_unrounded", Decimal("5000.000000"))
    values = {
        "tenant": household["tenant"],
        "payslip": payslip,
        "payroll_component": component,
        "component_code": component.code,
        "source_code": "3601",
        "description": "Basic wage",
        "units": Decimal("1.0000"),
        "rate": Decimal("5000.000000"),
        "component_type": "earning",
        "amount_unrounded": exact,
        "amount": exact.quantize(Decimal("0.01")),
    }
    values.update(overrides)
    with tenant_context(household["tenant"].pk):
        return PayslipLine.objects.create(**values)
