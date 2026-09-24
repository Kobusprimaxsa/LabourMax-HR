"""The COIDA accumulation against the database: which lines, which period,
which flag, which ceiling — and the tenant pin a return job depends on.

The arithmetic is ``calculators/tests/test_coida.py``'s, against the
Department's own examples. What is proved here is only what the caller adds.
"""

from __future__ import annotations

import contextlib
import datetime
from decimal import Decimal

import pytest
from django.db import connection, transaction

from core.db.rls import REFERENCE_MAINTENANCE_VAR
from core.managers import platform_context, tenant_context
from employers.models import PayrollComponent
from payroll import coida
from payroll.models import PayrollCalculationTrace
from payroll.tests.conftest import a_line, a_payslip, a_period, a_run
from statutory.models import StatutoryParameter
from statutory.resolve import StatutoryValueMissingError

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
END = datetime.date(2027, 2, 28)


@pytest.fixture
def ceiling(db):
    """Notice 3910 of 2026, GG 54577: "The amount of R668 000 per employee per
    annum … effective from 1st March 2026". Created here the way the household
    fixture creates the minimum age — tests do not load the fixture files."""
    return StatutoryParameter.objects.create(
        parameter_code=coida.CEILING_PARAMETER,
        value_numeric=Decimal("668000.000000"),
        unit=StatutoryParameter.Unit.ZAR,
        effective_from=START,
        source_reference="Compensation Fund maximum amount of earnings, 2026/2027",
    )


@pytest.fixture
def lump_sum_component(db) -> PayrollComponent:
    """A shared component outside the COIDA base, the way 3901 is loaded."""
    with transaction.atomic(), platform_context():
        return PayrollComponent.objects.create(
            code="LUMP",
            name="Lump sum",
            component_type=PayrollComponent.ComponentType.EARNING,
            calculation_method=PayrollComponent.CalculationMethod.FIXED,
            display_order=90,
            is_system=True,
            is_coida_base=False,
        )


def paid(household, tax_year, component, amount, *, number, payment, finalised=True):
    period = a_period(
        household,
        tax_year,
        number=number,
        start=payment - datetime.timedelta(days=27),
        payment=payment,
    )
    payslip = a_payslip(household, a_run(household, period), finalised=finalised)
    a_line(household, payslip, component, amount_exact=Decimal(amount))
    return payslip


def earnings(household, **kwargs):
    return coida.employee_earnings(
        household["employee"], period_start=START, period_end=END, calculated_for=END, **kwargs
    )


MARCH_PAY = datetime.date(2026, 3, 28)
APRIL_PAY = datetime.date(2026, 4, 25)


def test_finalised_lines_in_the_period_are_summed(household, tax_year, basic_component, ceiling):
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)
    paid(household, tax_year, basic_component, "5000", number=2, payment=APRIL_PAY)

    assert earnings(household).declared.rounded == Decimal("10000.00")


def test_a_draft_payslip_is_not_declared(household, tax_year, basic_component, ceiling):
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)
    paid(household, tax_year, basic_component, "7000", number=2, payment=APRIL_PAY, finalised=False)

    assert earnings(household).declared.rounded == Decimal("5000.00")


def test_the_payment_date_decides_the_period_on_both_sides_of_it(
    household, tax_year, basic_component, ceiling
):
    day = datetime.timedelta(days=1)
    paid(household, tax_year, basic_component, "1000", number=1, payment=END)
    paid(household, tax_year, basic_component, "9000", number=2, payment=END + day)
    paid(household, tax_year, basic_component, "9000", number=3, payment=START - day)

    assert earnings(household).declared.rounded == Decimal("1000.00")


def test_the_component_flag_is_read_not_restated(
    household, tax_year, basic_component, lump_sum_component, ceiling
):
    """D-89: the flag decides. Turning it on for the lump sum component — the
    data change an answer to O-06 or O-15 would be — moves the figure with no
    code change."""
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)
    paid(household, tax_year, lump_sum_component, "2000", number=2, payment=APRIL_PAY)

    before = earnings(household)
    assert (before.declared.rounded, before.excluded.rounded) == (
        Decimal("5000.00"),
        Decimal("2000.00"),
    )

    # A system row is locked against the platform too (D-93); deliberate
    # maintenance opens the lock by name, inside a transaction (D-92).
    with transaction.atomic(), platform_context(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        PayrollComponent.objects.filter(pk=lump_sum_component.pk).update(is_coida_base=True)

    assert earnings(household).declared.rounded == Decimal("7000.00")


def test_the_loaded_ceiling_caps_the_year_once(household, tax_year, basic_component, ceiling):
    for month in range(1, 13):
        paid(
            household,
            tax_year,
            basic_component,
            "60000",
            number=month,
            payment=MARCH_PAY + datetime.timedelta(days=28 * (month - 1)),
        )

    result = earnings(household)
    assert result.earnings.rounded == Decimal("720000.00")
    assert result.declared.rounded == Decimal("668000.00")
    assert tuple(result.trace.statutory_rows) == (("statutory_parameter", ceiling.pk),)


def test_no_loaded_ceiling_refuses_rather_than_declaring_uncapped(
    household, tax_year, basic_component
):
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)

    with pytest.raises(StatutoryValueMissingError, match="COIDA_ANNUAL_CEILING"):
        earnings(household)


def test_the_accumulation_pins_the_tenant_itself(household, tax_year, basic_component, ceiling):
    """A return is prepared by a job with no request, so nothing is pinned when
    this is called — which is how every test here calls it."""
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)
    assert earnings(household).declared.rounded == Decimal("5000.00")


def test_without_the_pin_the_same_call_declares_nil(
    household, tax_year, basic_component, ceiling, monkeypatch
):
    """PROVE EVERY GUARD FAILS: take the pin away and RLS returns no lines —
    reading as "nothing earned", the silent shape CLAUDE.md's table warns of."""
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)
    monkeypatch.setattr(coida, "tenant_context_of", lambda _row: contextlib.nullcontext())

    assert earnings(household).declared.rounded == Decimal("0.00")


def test_keeping_the_trace_writes_one_row(household, tax_year, basic_component, ceiling):
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)

    earnings(household, keep_trace=True)

    with tenant_context(household["tenant"].pk):
        (trace,) = PayrollCalculationTrace.objects.filter(calculator="coida.assessment_earnings")
    assert trace.outputs["declared"] == "5000.000000"
    assert trace.inputs["line_001"] == "3601|5000.000000|True"


def test_the_employer_total_is_the_sum_of_capped_employees(
    household, tax_year, basic_component, ceiling
):
    paid(household, tax_year, basic_component, "5000", number=1, payment=MARCH_PAY)
    paid(household, tax_year, basic_component, "-1000", number=2, payment=APRIL_PAY)

    declaration = coida.employer_earnings(
        household["employer"], period_start=START, period_end=END, calculated_for=END
    )

    (row,) = declaration.employees
    assert row.employee_id == household["employee"].pk
    assert declaration.total_declared == Decimal("4000.000000")
