"""Writing a calculator's trace. The CALLER's half of invariant 5 (D-208).

The calculator returns a ``calculators.base.CalculationTrace`` — pure, no
database. This is the only place that turns one into a row, so "did this
calculation get recorded" has one answer and not one per caller.

A trace is written for every calculation, including one that produced a zero or
a warning: a missing row must mean "this never ran".
"""

from __future__ import annotations

from django.db import transaction

from calculators.base import CalculationTrace
from core.managers import tenant_context_of
from payroll.models import PayrollCalculationTrace


def record(employee, trace: CalculationTrace, *, payslip_id_ref: int | None = None):
    """Persist one calculator's trace against one employee."""
    with transaction.atomic(), tenant_context_of(employee):
        return PayrollCalculationTrace.objects.create(
            tenant=employee.tenant,
            employee=employee,
            payslip_id_ref=payslip_id_ref,
            calculator=trace.calculator,
            calculated_for=trace.calculated_for,
            inputs=dict(trace.inputs),
            statutory_rows=[list(row) for row in trace.statutory_rows],
            outputs=dict(trace.outputs),
            warnings=list(trace.warnings),
        )
