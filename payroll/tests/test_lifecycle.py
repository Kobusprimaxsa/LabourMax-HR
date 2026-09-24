"""The pay period's lifecycle (D-305): the states, the moves, and what each refuses.

PROVE EVERY GUARD FAILS: every refusal below is watched refusing, message
asserted, and the scan at the bottom fails on a status written anywhere but
``lifecycle.transition()``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.managers import tenant_context
from payroll import lifecycle, runs
from payroll.models import PayPeriod, PayrollRun
from payroll.tests.conftest import a_period
from payroll.tests.test_runs import a_clean_run, to_calculated

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def somebody_is_owed_a_bonus(monkeypatch):
    """These tests are about the PERIOD. Whether an employee is owed a bonus is
    ``payroll/bonusrun.py``'s question (D-311), tested in test_bonus_run.py; the
    household fixture is domestic and owed none, so it is answered here."""
    monkeypatch.setattr("payroll.bonusrun.employees_owed", lambda period: ["someone"])


PeriodStatus = PayPeriod.Status
REPO = pathlib.Path(__file__).resolve().parents[2]


def reread(period):
    with tenant_context(period.tenant_id):
        period.refresh_from_db()
    return period


def finalised(run, approver):
    to_calculated(run)
    runs.approve(run, approved_by=approver)
    return runs.finalise(run, finalised_by=approver)


# ------------------------------------------------------------------ the map


def test_every_status_appears_in_the_transition_map():
    """A status missing from the map is terminal by accident."""
    assert set(lifecycle.LEGAL_PERIOD_TRANSITIONS) == set(PeriodStatus.values)


def test_an_illegal_move_is_refused_and_names_where_it_could_go(household, tax_year):
    period = a_period(household, tax_year)

    with pytest.raises(lifecycle.PeriodError, match="cannot go from open to closed.*in_progress"):
        lifecycle.transition(period, PeriodStatus.CLOSED)


def test_opening_a_run_moves_the_period_in_progress(household, tax_year):
    period = a_period(household, tax_year)
    runs.open_run(period)
    assert reread(period).status == PeriodStatus.IN_PROGRESS


# ------------------------------------------------------- the brief's two cases


def test_a_closed_period_refuses_a_new_run(household, tax_year, approver, basic_component):
    period = a_period(household, tax_year)
    report = finalised(
        a_clean_run(household, tax_year, approver, basic_component, period=period), approver
    )
    assert report.period_closed
    assert reread(period).status == PeriodStatus.CLOSED

    with pytest.raises(lifecycle.PeriodError, match="Period 1 is closed"):
        runs.open_run(period)
    with pytest.raises(lifecycle.PeriodError, match="Period 1 is closed"):
        runs.open_run(period, run_type=PayrollRun.RunType.BONUS)


def test_an_open_period_with_an_unfinalised_run_refuses_to_close(household, tax_year):
    period = a_period(household, tax_year)
    run = runs.open_run(period)

    with pytest.raises(
        lifecycle.PeriodError, match=r"cannot close while run 1 \(regular\) is draft"
    ):
        lifecycle.close(period)
    assert reread(period).status == PeriodStatus.IN_PROGRESS

    runs.transition(run, PayrollRun.Status.CALCULATING)
    runs.transition(run, PayrollRun.Status.CALCULATED)
    with pytest.raises(lifecycle.PeriodError, match="is calculated"):
        lifecycle.close(period)


# ------------------------------------------------------------ the bonus run


def test_a_bonus_run_may_be_live_beside_the_regular_run(household, tax_year):
    """A bonus pays for no days, so it does not clash with the run that does."""
    period = a_period(household, tax_year)
    runs.open_run(period)

    bonus = runs.open_run(period, run_type=PayrollRun.RunType.BONUS)

    assert (bonus.run_number, bonus.run_type) == (2, "bonus")


def test_a_second_live_bonus_run_is_refused(household, tax_year):
    period = a_period(household, tax_year)
    runs.open_run(period, run_type=PayrollRun.RunType.BONUS)

    with pytest.raises(runs.PayrollRunError, match="would each pay the same bonus"):
        runs.open_run(period, run_type=PayrollRun.RunType.BONUS)


def test_finalising_one_run_leaves_the_period_open_under_another_live_one(
    household, tax_year, approver, basic_component
):
    """D-235 as amended by D-305: the LAST live run closes the period."""
    period = a_period(household, tax_year)
    regular = a_clean_run(household, tax_year, approver, basic_component, period=period)
    runs.open_run(period, run_type=PayrollRun.RunType.BONUS)

    report = finalised(regular, approver)

    assert not report.period_closed
    assert reread(period).status == PeriodStatus.IN_PROGRESS


# ---------------------------------------------------------- reopening


def test_reversal_reopens_the_period_and_counts_it(household, tax_year, approver, basic_component):
    period = a_period(household, tax_year)
    run = a_clean_run(household, tax_year, approver, basic_component, period=period)
    finalised(run, approver)

    runs.reverse(run, reversed_by=approver, reason="Wrong rate")

    period = reread(period)
    assert (period.status, period.reopened_count, period.closed_at) == (
        PeriodStatus.REOPENED,
        1,
        None,
    )


def test_a_reopened_period_closes_again_once_its_replacement_is_final(
    household, tax_year, approver, basic_component
):
    period = a_period(household, tax_year)
    run = a_clean_run(household, tax_year, approver, basic_component, period=period)
    finalised(run, approver)
    runs.reverse(run, reversed_by=approver, reason="Wrong rate")

    lifecycle.close(period)

    assert reread(period).status == PeriodStatus.CLOSED


# -------------------------------------------------- the only way a period moves


def _status_writes(tree):
    """Every ``<something>.status = ...`` on a name that reads as a period."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                    and "period" in target.value.id
                ):
                    yield node.lineno


def test_no_code_but_the_lifecycle_writes_a_periods_status():
    offences = []
    for package in ("payroll", "employers", "attendance", "core"):
        for path in (REPO / package).rglob("*.py"):
            if {"tests", "migrations"} & set(path.parts) or path.name == "lifecycle.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offences += [f"{path.relative_to(REPO)}:{line}" for line in _status_writes(tree)]
    assert not offences, f"A period's status written outside lifecycle.transition(): {offences}"


def test_the_scan_finds_a_status_written_directly():
    """PROVE EVERY GUARD FAILS: the scan sees the shape it exists for."""
    tree = ast.parse("period.status = 'closed'\npay_period.status = x\nrun.status = y\n")
    assert list(_status_writes(tree)) == [1, 2]


def test_a_refused_bonus_run_leaves_the_period_as_it_was(household, tax_year, monkeypatch):
    """The refusal comes before the period moves: an open period stays OPEN
    when the bonus run is refused, rather than in progress under no run."""
    monkeypatch.setattr("payroll.bonusrun.employees_owed", lambda period: [])
    period = a_period(household, tax_year)

    with pytest.raises(runs.PayrollRunError, match="Nobody on this pay group is owed a bonus"):
        runs.open_run(period, run_type=PayrollRun.RunType.BONUS)
    assert reread(period).status == PeriodStatus.OPEN
