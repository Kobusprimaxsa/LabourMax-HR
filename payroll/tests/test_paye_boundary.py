"""Where the pure PAYE calculator meets the database (D-212).

Two things are checked here and nowhere else, because neither side can check
them alone:

1. ``calculators.paye.TaxStatus`` is a COPY of
   ``employees.EmployeeTaxProfile.TaxStatus``. A calculator may not import a
   model, so the copy is unavoidable; a copy nobody compares is how the two
   drift apart and a directive silently stops being recognised as one.
2. What the calculator's input needs, the reference data and the tax profile can
   actually supply — the bracket's four columns, the rebate tiers for an age,
   the medical credit's three, and a directive's percentage or amount.

The trace itself is written through ``payroll.trace.record()``, exactly as UIF's
and SDL's are (D-208): the calculator produces the structure, the caller
persists it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.paye import (
    DIRECTIVE_STATUSES,
    MedicalCredit,
    PayeInput,
    TaxBracket,
    TaxStatus,
    employees_tax,
)
from core.managers import tenant_context
from employees.models import EmployeeTaxProfile
from payroll.models import PayrollCalculationTrace
from payroll.tests.test_calculation_trace import employee  # noqa: F401 — fixture
from payroll.trace import record
from statutory import resolve
from statutory.models import (
    MedicalTaxCreditRate,
    PayeRebate,
    PayeTaxBracket,
    TaxYear,
)

MARCH = datetime.date(2026, 3, 31)


# ------------------------------------------------------------- the two enums


def test_the_calculator_knows_exactly_the_tax_statuses_the_tax_profile_can_hold():
    """A status the calculator has never heard of would fall through to the
    ordinary tables — which is the right answer for FOREIGN and the wrong one
    for anything that turns out to be a directive."""
    assert {status.value for status in TaxStatus} == set(EmployeeTaxProfile.TaxStatus.values)


def test_the_two_sides_agree_on_which_statuses_are_directives():
    assert {status.value for status in DIRECTIVE_STATUSES} == {
        status.value for status in EmployeeTaxProfile.DIRECTIVE_STATUSES
    }


# ------------------------------------------- the reference data fits the input


@pytest.mark.django_db
def test_a_loaded_tax_year_assembles_into_a_calculator_input():
    """Not a golden test — a wiring test. Every column the calculator's input
    structure needs must exist on the rows ``statutory.resolve`` hands back, or
    the assembly cannot be built on top of it when P2 is finally verified.
    """
    year = TaxYear.objects.create(
        label="2026/2027",
        start_date=datetime.date(2026, 3, 1),
        end_date=datetime.date(2027, 2, 28),
    )
    PayeTaxBracket.objects.create(
        tax_year=year,
        bracket_order=1,
        income_from=Decimal("0"),
        income_to=Decimal("245100"),
        base_tax=Decimal("0"),
        marginal_rate_pct=Decimal("18.00"),
        source_reference="Income Tax Act 58 of 1962, s5; Rates and Monetary Amounts Act",
    )
    PayeTaxBracket.objects.create(
        tax_year=year,
        bracket_order=2,
        income_from=Decimal("245100"),
        income_to=None,
        base_tax=Decimal("44118"),
        marginal_rate_pct=Decimal("26.00"),
        source_reference="Income Tax Act 58 of 1962, s5; Rates and Monetary Amounts Act",
    )
    PayeRebate.objects.create(
        tax_year=year,
        rebate_type=PayeRebate.RebateType.PRIMARY,
        min_age=0,
        annual_amount=Decimal("17820"),
        tax_threshold_annual=Decimal("99000"),
        source_reference="Income Tax Act 58 of 1962, s6(2)(a)",
    )
    MedicalTaxCreditRate.objects.create(
        tax_year=year,
        main_member_monthly=Decimal("376.00"),
        first_dependant_monthly=Decimal("376.00"),
        additional_dependant_monthly=Decimal("254.00"),
        source_reference="Income Tax Act 58 of 1962, s6A(2)(b)",
    )

    rows = resolve.paye_brackets(year)
    credit_row = resolve.medical_tax_credit(year)

    data = PayeInput(
        calculated_for=MARCH,
        remuneration=Decimal("20000.00"),
        allowable_deductions=Decimal("0.00"),
        annual_payment=Decimal("0.00"),
        periods_in_year=Decimal("12"),
        periods_worked=Decimal("1"),
        brackets=tuple(
            TaxBracket(
                income_from=row.income_from,
                income_to=row.income_to,
                base_tax=row.base_tax,
                marginal_rate_percent=row.marginal_rate_pct,
                table=PayeTaxBracket._meta.db_table,
                row_id=row.pk,
            )
            for row in rows
        ),
        rebates=tuple(
            StatutoryFigure(
                value=row.annual_amount,
                table=PayeRebate._meta.db_table,
                row_id=row.pk,
                description=row.get_rebate_type_display(),
            )
            for row in resolve.paye_rebates(year, age=40)
        ),
        medical_scheme_members=2,
        medical_credit=MedicalCredit(
            main_member_monthly=credit_row.main_member_monthly,
            first_dependant_monthly=credit_row.first_dependant_monthly,
            additional_dependant_monthly=credit_row.additional_dependant_monthly,
            table=MedicalTaxCreditRate._meta.db_table,
            row_id=credit_row.pk,
        ),
    )

    result = employees_tax(data)

    # 20 000 × 12 = 240 000; 44 118 + 26% of 240 000 − 245 100 is below the band,
    # so 18% of 240 000 = 43 200, less 17 820, less 752 × 12.
    assert result.annual_tax_before_credits.exact == Decimal("43200.000000")
    assert result.annual_tax_after_credits.exact == Decimal("16356.000000")
    assert result.tax.rounded == Decimal("1363.00")
    assert set(result.trace.statutory_rows) == {
        (PayeTaxBracket._meta.db_table, rows[0].pk),
        (PayeTaxBracket._meta.db_table, rows[1].pk),
        (PayeRebate._meta.db_table, PayeRebate.objects.get().pk),
        (MedicalTaxCreditRate._meta.db_table, credit_row.pk),
    }


# -------------------------------------------------------------- and it stores


@pytest.mark.django_db
def test_a_paye_trace_is_written_through_the_same_boundary_as_uif_and_sdl(employee):  # noqa: F811
    result = employees_tax(
        PayeInput(
            calculated_for=MARCH,
            remuneration=Decimal("20000.00"),
            allowable_deductions=Decimal("0.00"),
            annual_payment=Decimal("0.00"),
            periods_in_year=Decimal("12"),
            periods_worked=Decimal("1"),
            brackets=(
                TaxBracket(
                    income_from=Decimal("0"),
                    income_to=None,
                    base_tax=Decimal("0"),
                    marginal_rate_percent=Decimal("18"),
                    table="paye_tax_bracket",
                    row_id=701,
                ),
            ),
            rebates=(StatutoryFigure(value=Decimal("17820"), table="paye_rebate", row_id=711),),
        )
    )

    row = record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)
    assert stored.calculator_name == "paye.employees_tax"
    assert stored.outputs["annual_equivalent"] == "240000.000000"
    assert stored.reference_rows_used == [["paye_rebate", 711], ["paye_tax_bracket", 701]]
