"""The forfeiture deadline warning — task 3, task 6. A query, not a screen
and not a job: it writes nothing, and every bucket boundary is proven by a
cycle placed exactly either side of it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from employees.engagements import terminate
from leave.cycles import ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveCycle, LeaveTransaction
from leave.warnings import Bucket, forfeiture_warnings

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType
MONDAY = datetime.date(2026, 3, 2)


def _grant_balance(employee, leave_type, *, quantity: Decimal, on_date):
    cycle = ensure_cycles(employee, leave_type, horizon=on_date)[0]
    post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=leave_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=quantity,
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=on_date,
        calculation_basis="manual",
    )
    return cycle


def test_a_cycle_that_has_not_ended_yet_is_not_warned_about(
    employer, employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"), on_date=MONDAY)

    results = forfeiture_warnings(employer, as_at=datetime.date(2026, 6, 1))

    assert results == [], "cycle_end is 2027-03-01 — nowhere near ended yet."


def test_a_cycle_with_zero_balance_is_not_warned_about(
    employer, employee, engagement, leave_rules, annual_type, schedule_5day
):
    ensure_cycles(employee, annual_type, horizon=MONDAY)

    results = forfeiture_warnings(employer, as_at=datetime.date(2027, 6, 1))

    assert results == []


def test_an_ended_cycle_with_a_balance_is_bucketed_approaching(
    employer, employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("10.000"), on_date=MONDAY)
    cycle_end = datetime.date(2027, 3, 1)  # cycle_months=12 from 2026-03-01

    # Forfeit deadline = cycle_end + 6 months = 2027-09-01. Far from it.
    as_at = cycle_end + datetime.timedelta(days=10)

    results = forfeiture_warnings(employer, as_at=as_at)

    assert len(results) == 1
    warning = results[0]
    assert warning.bucket == Bucket.APPROACHING
    assert warning.balance == Decimal("10.000")
    assert warning.forfeit_deadline == datetime.date(2027, 9, 1)
    assert warning.days_remaining > 30


def test_a_cycle_inside_the_due_window_is_bucketed_due(
    employer, employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("10.000"), on_date=MONDAY)
    deadline = datetime.date(2027, 9, 1)

    as_at = deadline - datetime.timedelta(days=15)

    results = forfeiture_warnings(employer, as_at=as_at)

    assert len(results) == 1
    assert results[0].bucket == Bucket.DUE
    assert 0 <= results[0].days_remaining <= 30


def test_a_cycle_past_its_deadline_is_bucketed_past(
    employer, employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("10.000"), on_date=MONDAY)
    deadline = datetime.date(2027, 9, 1)

    as_at = deadline + datetime.timedelta(days=1)

    results = forfeiture_warnings(employer, as_at=as_at)

    assert len(results) == 1
    assert results[0].bucket == Bucket.PAST
    assert results[0].days_remaining < 0


def test_a_terminated_employees_closed_cycle_is_not_warned_about(
    employer, employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    """A closed cycle is a termination-payout question (BCEA s40(b)), not a
    forfeiture one — including it here would conflate the two duties."""
    _grant_balance(employee, annual_type, quantity=Decimal("10.000"), on_date=MONDAY)
    terminate(engagement, termination_date=datetime.date(2026, 8, 31), reason_code="resigned")

    results = forfeiture_warnings(employer, as_at=datetime.date(2028, 1, 1))

    assert results == []


def test_results_are_ordered_most_urgent_first(
    employer, tenant, employee, engagement, leave_rules, annual_type, schedule_5day, minimum_age
):
    from core.managers import tenant_context
    from employees.engagements import engage
    from employees.identity import luhn_check_digit
    from employees.models import Employee
    from leave.tests.conftest import BORN

    with tenant_context(tenant.pk):
        body = "9001015010"
        second_employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Palesa",
            last_name="Dlamini",
            date_of_birth=BORN,
            mobile_number="+27820000099",
            email="palesa@example.com",
            id_number=body + str(luhn_check_digit(body)),
        )
    engage(second_employee, start_date=datetime.date(2025, 3, 2), job_title="Domestic worker")

    # employee's own cycle ends 2027-03-01, deadline 2027-09-01.
    _grant_balance(employee, annual_type, quantity=Decimal("10.000"), on_date=MONDAY)
    # second_employee's cycle ends a year earlier: 2026-03-02, deadline 2026-09-02
    # — further past its own deadline by the time we check, so it must sort first.
    _grant_balance(
        second_employee,
        annual_type,
        quantity=Decimal("5.000"),
        on_date=datetime.date(2025, 3, 2),
    )

    as_at = datetime.date(2027, 9, 5)  # past both cycles' own deadlines, second's by longer
    results = forfeiture_warnings(employer, as_at=as_at)

    assert [w.employee_id for w in results] == [second_employee.pk, employee.pk]
    assert results[0].days_remaining < results[1].days_remaining
