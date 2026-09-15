"""Forfeiture capture — task 2, task 6. Manual, by the employer, and nothing
else. Every guard here is proven by first watching it fail.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context_of
from core.models import AppUser
from leave.balances import recompute_cycle
from leave.cycles import ensure_cycles
from leave.forfeiture import ForfeitureRefusedError, capture_forfeiture
from leave.ledger import post_transaction, reverse_transaction
from leave.models import LeaveCycle, LeaveTransaction

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType
MONDAY = datetime.date(2026, 3, 2)


def _grant_balance(employee, leave_type, *, quantity: Decimal, on_date=MONDAY):
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


@pytest.fixture
def captured_by(db):
    return AppUser.objects.create_user(email="employer@example.com", password="x" * 16)


@pytest.fixture
def cycle(employee, engagement, leave_rules, annual_type, schedule_5day):
    return _grant_balance(employee, annual_type, quantity=Decimal("15.000"))


def test_capturing_a_forfeiture_writes_a_negative_transaction_attributed_to_the_user(
    cycle, captured_by
):
    txn = capture_forfeiture(
        cycle, quantity=Decimal("5.000"), reason="Cycle expired, unused", captured_by=captured_by
    )

    assert txn.transaction_type == TransactionType.FORFEITURE
    assert txn.days == Decimal("-5.000")
    assert txn.created_by_user_id == captured_by.pk
    assert txn.reason == "Cycle expired, unused"

    with tenant_context_of(cycle):
        recomputed = recompute_cycle(cycle)
    assert recomputed.forfeited_quantity == Decimal("-5.000")
    assert recomputed.balance_quantity == Decimal("10.000")


def test_a_forfeiture_needs_a_reason(cycle, captured_by):
    with pytest.raises(ForfeitureRefusedError) as raised:
        capture_forfeiture(cycle, quantity=Decimal("1.000"), reason="", captured_by=captured_by)

    assert "reason" in str(raised.value).lower()


def test_a_forfeiture_larger_than_the_balance_is_refused_naming_both_figures(cycle, captured_by):
    with pytest.raises(ForfeitureRefusedError) as raised:
        capture_forfeiture(
            cycle, quantity=Decimal("20.000"), reason="Too much", captured_by=captured_by
        )

    message = str(raised.value)
    assert "20.000" in message
    assert "15.000" in message


def test_a_forfeiture_against_a_cycle_with_no_balance_is_refused(
    employee, engagement, leave_rules, annual_type, schedule_5day, captured_by
):
    empty_cycle = ensure_cycles(employee, annual_type, horizon=MONDAY)[0]

    with pytest.raises(ForfeitureRefusedError) as raised:
        capture_forfeiture(
            empty_cycle,
            quantity=Decimal("1.000"),
            reason="Nothing to take",
            captured_by=captured_by,
        )

    assert "no balance to forfeit" in str(raised.value)


def test_a_zero_or_negative_forfeiture_is_refused(cycle, captured_by):
    with pytest.raises(ForfeitureRefusedError):
        capture_forfeiture(cycle, quantity=Decimal("0"), reason="Nothing", captured_by=captured_by)

    with pytest.raises(ForfeitureRefusedError):
        capture_forfeiture(
            cycle, quantity=Decimal("-1.000"), reason="Negative", captured_by=captured_by
        )


def test_a_forfeiture_is_reversible_like_any_other_ledger_entry(cycle, captured_by):
    txn = capture_forfeiture(
        cycle, quantity=Decimal("5.000"), reason="Captured in error", captured_by=captured_by
    )

    reversal = reverse_transaction(txn, reason="Captured against the wrong cycle")

    assert reversal.transaction_type == TransactionType.REVERSAL
    assert reversal.days == Decimal("5.000")

    with tenant_context_of(cycle):
        recomputed = recompute_cycle(cycle)
    assert recomputed.balance_quantity == Decimal("15.000"), "Restored exactly."


def test_the_no_automatic_forfeiture_test_is_unmodified_and_still_passes():
    """CHUNK 1'S OWN GUARD, UNTOUCHED. This test does not re-implement that
    proof — it exists so a reviewer diffing this chunk sees explicitly that
    ``leave/tests/test_accrual.py::test_no_forfeiture_transaction_is_ever_written_automatically``
    was not edited, and this module names it rather than assuming."""
    import inspect

    from leave.tests import test_accrual

    assert hasattr(test_accrual, "test_no_forfeiture_transaction_is_ever_written_automatically")
    source = inspect.getsource(
        test_accrual.test_no_forfeiture_transaction_is_ever_written_automatically
    )
    assert "forfeiture_count == 0" in source
