"""The COIDA accumulation: finalised payslips in, one capped figure per employee.

The caller's half of ``calculators/coida.py`` — read the rows, hand the pure
function its inputs, record the trace. P8's ``coida_return_of_earnings`` is the
consumer; nothing here files, assesses or stores a return.

**Which payslips count.** Only FINALISED ones, for the same reason as
``payroll/ytd.py``: a draft moves before anybody is paid. A reversing payslip
counts, and nets off, because its lines are negative. A payslip belongs to the
assessment period its pay period's PAYMENT DATE falls in — the CF-2A declares
earnings "paid", and D-83 already puts a period in the tax year of its payment
date, so one date answers both questions.

**What counts as earnings is ``payroll_component.is_coida_base``, read off each
line's component** (D-89). The flag is copied from the SARS source code and
``PayrollComponent.clean()`` refuses any other combination, so this is still
decided in one place. Read at accumulation time, not frozen on the line: a flag
corrected after O-06 answers the 3607 question moves every return computed after
the correction, which is what a correction is for — and the trace records the
flag each line was counted under, so an earlier return can still be explained.

**The ceiling is resolved on the period's FIRST day.** The notice makes it
"effective from 1st March", the first day of the period it caps, and a ceiling
gazetted mid-period does not re-cap the months before it.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from calculators.base import StatutoryFigure
from calculators.coida import CoidaEarning, CoidaInput, CoidaResult, assessment_earnings
from core.managers import tenant_context_of
from payroll.models import PayslipLine
from payroll.trace import record
from statutory import resolve

CEILING_PARAMETER = "COIDA_ANNUAL_CEILING"


@dataclasses.dataclass(frozen=True)
class EmployeeDeclaration:
    employee_id: int
    result: CoidaResult


@dataclasses.dataclass(frozen=True)
class EmployerDeclaration:
    """Every employee's capped figure, and the employer's total of them."""

    period_start: datetime.date
    period_end: datetime.date
    employees: tuple[EmployeeDeclaration, ...]

    @property
    def total_declared(self) -> Decimal:
        return sum((row.result.declared.exact for row in self.employees), Decimal("0"))


def _ceiling(period_start: datetime.date) -> StatutoryFigure:
    row = resolve.parameter(CEILING_PARAMETER, period_start)
    return StatutoryFigure(
        value=row.value_numeric,
        table="statutory_parameter",
        row_id=row.pk,
        description=row.source_reference,
    )


def _lines(employee, period_start, period_end):
    return (
        PayslipLine.objects.filter(
            payslip__employee=employee,
            payslip__is_finalised=True,
            payslip__payroll_run__pay_period__payment_date__gte=period_start,
            payslip__payroll_run__pay_period__payment_date__lte=period_end,
        )
        .select_related("payroll_component")
        .order_by("payslip__payroll_run__pay_period__payment_date", "payslip_id", "sequence", "pk")
    )


def employee_earnings(
    employee,
    *,
    period_start: datetime.date,
    period_end: datetime.date,
    calculated_for: datetime.date,
    keep_trace: bool = False,
) -> CoidaResult:
    """One employee's declarable earnings. Pins the employee's own tenant — a
    return is prepared by a job with no request behind it, and an unpinned
    read of payslip lines returns none and declares nil (CLAUDE.md's table)."""
    with tenant_context_of(employee):
        earnings = tuple(
            CoidaEarning(
                source_code=line.source_code,
                amount=line.amount_exact,
                is_coida_base=line.payroll_component.is_coida_base,
            )
            for line in _lines(employee, period_start, period_end)
        )
        result = assessment_earnings(
            CoidaInput(
                calculated_for=calculated_for,
                assessment_period_start=period_start,
                assessment_period_end=period_end,
                annual_ceiling=_ceiling(period_start),
                earnings=earnings,
            )
        )
    if keep_trace:
        record(employee, result.trace)
    return result


def employer_earnings(
    employer,
    *,
    period_start: datetime.date,
    period_end: datetime.date,
    calculated_for: datetime.date,
) -> EmployerDeclaration:
    """Every employee with a finalised payslip paid in the period — a leaver
    included, since their earnings were paid in it — capped one at a time."""
    from employees.models import Employee

    with tenant_context_of(employer):
        employees = list(
            Employee.objects.filter(
                employer=employer,
                payslips__is_finalised=True,
                payslips__payroll_run__pay_period__payment_date__gte=period_start,
                payslips__payroll_run__pay_period__payment_date__lte=period_end,
            )
            .distinct()
            .order_by("pk")
        )
    return EmployerDeclaration(
        period_start=period_start,
        period_end=period_end,
        employees=tuple(
            EmployeeDeclaration(
                employee_id=employee.pk,
                result=employee_earnings(
                    employee,
                    period_start=period_start,
                    period_end=period_end,
                    calculated_for=calculated_for,
                ),
            )
            for employee in employees
        ),
    )
