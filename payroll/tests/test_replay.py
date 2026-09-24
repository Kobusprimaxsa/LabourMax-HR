"""``payroll/replay.py`` (D-313): PROVE EVERY GUARD FAILS.

The end-to-end proof asserts every payslip reproduces from its traces. That is
only worth something if a payslip that does NOT reproduce is caught — so each
way a stored trace can disagree with a re-run is watched being reported here.
The hand-computed leaver is used because a termination payslip carries the most
calculators: gross, the payout, PAYE, UIF, SDL and net.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.managers import tenant_context
from payroll import replay, runs, termination
from payroll.models import PayrollCalculationTrace
from payroll.tests.test_termination_payslip import (
    a_cleaner,
    a_cleaning_employer,
    annual_leave,
    june,
    load_shipped,
    only_payslip,
    resigns_paid_in_lieu,
    worked_first_ten_days_of_june,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def leavers_payslip(db):
    tax_year, person = load_shipped()
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    annual_leave(employee, first_cycle_taken=Decimal("12"), second_cycle_accrued=Decimal("1.25"))
    engagement = resigns_paid_in_lieu(engagement)
    termination.review(termination.prepare(engagement), reviewed_by=person)
    run = runs.calculate(runs.open_run(june(group, tax_year)))
    payslip, _ = only_payslip(run)
    return payslip


def traces(payslip):
    with tenant_context(payslip.tenant_id):
        return {
            row.calculator_name: row
            for row in PayrollCalculationTrace.objects.filter(payslip=payslip)
        }


def test_the_leavers_payslip_reproduces_and_every_calculator_was_replayed(leavers_payslip):
    assert set(traces(leavers_payslip)) == {
        "gross.gross_pay",
        "termination.termination_payout",
        "paye.employees_tax",
        "uif.contribution",
        "sdl.levy",
        "net.net_pay",
    }
    assert replay.reproduce(leavers_payslip) == []


def altered(payslip, calculator, **changes):
    """Every trace on the payslip, with one of them altered IN MEMORY. The stored
    trace cannot be edited — the table is append-only by trigger, which the
    first test below watches refusing."""
    rows = sorted(traces(payslip).values(), key=lambda row: row.sequence)
    for row in rows:
        if row.calculator_name == calculator:
            for name, value in changes.items():
                setattr(row, name, value)
    return rows


def test_a_stored_trace_cannot_be_edited_at_all(leavers_payslip):
    from django.db import ProgrammingError, transaction

    row = traces(leavers_payslip)["paye.employees_tax"]
    with (
        pytest.raises(ProgrammingError, match="append-only: a trace is evidence"),
        transaction.atomic(),
        tenant_context(row.tenant_id),
    ):
        PayrollCalculationTrace.objects.filter(pk=row.pk).update(outputs={})


def test_a_stored_output_that_the_rerun_does_not_produce_is_reported(leavers_payslip):
    row = traces(leavers_payslip)["termination.termination_payout"]
    rows = altered(
        leavers_payslip,
        "termination.termination_payout",
        outputs={**row.outputs, "notice_pay": "7300.000000"},
    )

    with tenant_context(leavers_payslip.tenant_id):
        (mismatch,) = replay.compare(rows)

    assert mismatch.calculator == "termination.termination_payout"
    assert "'notice_pay': '7200.000000'" in mismatch.what


def test_an_input_that_does_not_produce_the_stored_outputs_is_reported(leavers_payslip):
    row = traces(leavers_payslip)["paye.employees_tax"]
    rows = altered(
        leavers_payslip, "paye.employees_tax", inputs={**row.inputs, "remuneration": "10900.00"}
    )

    with tenant_context(leavers_payslip.tenant_id):
        (mismatch,) = replay.compare(rows)
    assert mismatch.calculator == "paye.employees_tax"


def test_a_recorded_row_the_rerun_does_not_read_is_reported(leavers_payslip):
    row = traces(leavers_payslip)["termination.termination_payout"]
    extra = [*row.reference_rows_used, ["statutory_parameter", 999999]]
    rows = altered(leavers_payslip, "termination.termination_payout", reference_rows_used=extra)

    with tenant_context(leavers_payslip.tenant_id):
        (mismatch,) = replay.compare(rows)
    assert "statutory_parameter row(s) [999999] recorded by the trace no longer exist" in (
        mismatch.what
    )


def test_a_calculator_the_replay_does_not_know_is_reported_never_skipped(leavers_payslip):
    rows = altered(leavers_payslip, "sdl.levy", calculator_name="sdl.new_levy")

    with tenant_context(leavers_payslip.tenant_id):
        (mismatch,) = replay.compare(rows)
    assert "No replay for sdl.new_levy; a trace is never skipped" in mismatch.what


def test_a_payslip_with_no_traces_does_not_reproduce():
    (mismatch,) = replay.compare([])
    assert mismatch.what == "the payslip has no traces at all"
