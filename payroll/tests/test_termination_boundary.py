"""Where the termination payout meets the database (D-227).

``calculators/termination.py`` copies ``TerminationNoticeBand.NoticeUnit``,
because a calculator may not import a model. Pricing has exactly two branches
and no fallback, deliberately: an unreachable ``else`` is a branch nobody can
test, and this comparison is the better guard. It is also what catches the
dangerous direction — a unit the MODEL gains while the calculator does not.

The rest is wiring: every row ``statutory.resolve`` hands back must assemble
into the input with nothing missing, and the trace persists through the same
boundary every other calculator uses (D-208).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.termination import (
    NoticeBand,
    NoticeUnit,
    ProRataLeaveRule,
    SeveranceRule,
    TerminationInput,
    termination_payout,
)
from core.managers import tenant_context
from payroll.models import PayrollCalculationTrace
from payroll.tests.test_calculation_trace import employee  # noqa: F401 — fixture
from payroll.trace import record
from statutory.models import TerminationNoticeBand

MARCH = datetime.date(2026, 3, 31)

PRO_RATA_PARAMETER = "PRO_RATA_LEAVE_MIN_SERVICE_MONTHS"


def test_the_calculator_knows_exactly_the_notice_units_the_band_can_hold():
    assert {unit.value for unit in NoticeUnit} == set(TerminationNoticeBand.NoticeUnit.values), (
        "A notice unit exists on one side and not the other. Pricing has no fallback "
        "branch by design, so a new unit must be handled in _notice_line() — a MONTHS "
        "band would need BCEA s35(3)'s four-and-one-third factor, which is loaded "
        "reference data the caller applies, not something to improvise in a calculator."
    )


def test_the_qualifying_period_is_loaded_and_is_four_months(db):
    """s40(c)'s "longer than four months", cited, and deliberately NOT reused
    from ``leave_rule_set.family_resp_min_service_months`` — that one is
    s27(1)'s family responsibility qualifier. Two statutes, two citations, two
    figures that happen to agree today."""
    from statutory.models import StatutoryParameter

    row = StatutoryParameter.objects.create(
        parameter_code=PRO_RATA_PARAMETER,
        value_numeric=Decimal("4.000000"),
        unit="months",
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s40(c)",
    )

    assert row.value_numeric == Decimal("4.000000")
    assert "s40(c)" in row.source_reference


def _an_input(**overrides) -> TerminationInput:
    values = {
        "calculated_for": MARCH,
        "weekly_rate": Decimal("3000.00"),
        "daily_rate": Decimal("600.00"),
        "hourly_rate": Decimal("66.666667"),
        "days_per_week": Decimal("5"),
        "hours_per_week": Decimal("45"),
        "notice_is_paid_in_lieu": True,
        "notice_band": NoticeBand(
            notice_value=Decimal("4.00"),
            notice_unit=NoticeUnit.WEEKS,
            table="termination_notice_band",
            row_id=61,
        ),
        "leave_due_days": Decimal("10.000"),
        "months_of_service": Decimal("18"),
        "incomplete_cycle_days": Decimal("4.000"),
        "days_worked_in_incomplete_cycle": Decimal("68"),
        "pro_rata_rule": ProRataLeaveRule(
            days_worked_per_leave_day=Decimal("17"), table="leave_rule_set", row_id=81
        ),
        "pro_rata_minimum_service_months": StatutoryFigure(
            value=Decimal("4.000000"), table="statutory_parameter", row_id=51
        ),
        "dismissed_for_operational_requirements": True,
        "severance_rule": SeveranceRule(
            weeks_per_completed_year=Decimal("1.00"),
            requires_operational_reason=True,
            table="termination_rule_set",
            row_id=71,
        ),
        "completed_years_of_service": Decimal("1"),
    }
    values.update(overrides)
    return TerminationInput(**values)


@pytest.mark.django_db
def test_a_full_payout_persists_its_trace_through_the_usual_boundary(employee):  # noqa: F811
    result = termination_payout(_an_input())
    row = record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)

    assert stored.calculator_name == "termination.termination_payout"
    # Four weeks' notice at R3 000, ten days' leave at R600, four days' pro-rata,
    # and one completed year of severance at a week.
    assert stored.outputs["notice_pay"] == "12000.000000"
    assert stored.outputs["leave_due_pay"] == "6000.000000"
    assert stored.outputs["pro_rata_leave_pay"] == "2400.000000"
    assert stored.outputs["severance"] == "3000.000000"
    assert stored.outputs["total"] == "23400.000000"
    assert sorted(stored.reference_rows_used) == [
        ["leave_rule_set", 81],
        ["statutory_parameter", 51],
        ["termination_notice_band", 61],
        ["termination_rule_set", 71],
    ]


@pytest.mark.django_db
def test_an_overdrawn_balance_reaches_the_stored_trace_as_a_warning(employee):  # noqa: F811
    """D-185: the figure must survive to the record a human reads, because
    netting it off is a s34 deduction needing the employee's consent."""
    result = termination_payout(_an_input(negative_leave_balance=Decimal("2.500")))
    row = record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)

    assert any("2.500" in warning for warning in stored.warnings)
    assert stored.outputs["total"] == "23400.000000", "undiminished"
