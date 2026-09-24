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


def record(employee, trace: CalculationTrace, *, payslip=None, sequence: int = 0):
    """Persist one calculator's trace against one employee.

    ``payslip`` is None for a calculation run outside a payslip — every test
    here, and any what-if a screen runs. It became a real foreign key when the
    payslip table was built (D-208 said it would).
    """
    with transaction.atomic(), tenant_context_of(employee):
        return PayrollCalculationTrace.objects.create(
            tenant=employee.tenant,
            employee=employee,
            payslip=payslip,
            calculator_name=trace.calculator,
            sequence=sequence,
            calculated_for=trace.calculated_for,
            inputs=dict(trace.inputs),
            reference_rows_used=[list(row) for row in trace.statutory_rows],
            outputs=dict(trace.outputs),
            warnings=list(trace.warnings),
        )
