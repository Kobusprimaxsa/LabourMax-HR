"""``leave_transaction`` — append-only by trigger, sign-checked by CHECK,
corrected only by reversal (task 3, task 6).

Every refusal here is proven by first watching it fail, and every assertion
reads the message, not only the exception class (D-134's own lesson).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction

from core.managers import tenant_context_of
from leave.cycles import ensure_cycles
from leave.ledger import LedgerRefusedError, post_transaction, reverse_transaction
from leave.models import LeaveCycle, LeaveTransaction

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType


@pytest.fixture
def cycle(employee, engagement, leave_rules, annual_type, schedule_5day):
    return ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))[0]


def test_post_transaction_writes_a_positive_accrual(employee, annual_type, cycle):
    txn = post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("1.250"),
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=datetime.date(2026, 3, 31),
        calculation_basis="monthly_1_25",
    )

    assert txn.pk is not None
    assert txn.quantity == Decimal("1.250")


def test_a_wrong_signed_accrual_is_refused_by_the_check(employee, annual_type, cycle):
    """THE SIGN CONVENTION, enforced by the database and not only implied.

    ``post_transaction`` calls ``full_clean()`` before ``save()``, and Django
    validates a model's own CHECK constraints as part of that (its own
    docstring says so) — so the refusal here is a ``ValidationError`` naming
    the constraint, not an ``IntegrityError`` from the database itself. The
    raw-``.create()`` test below reaches the database CHECK directly and
    proves THAT layer independently.
    """
    with pytest.raises(ValidationError) as raised:
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("-1.250"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 3, 31),
        )

    assert "sign_matches_type" in str(raised.value)


def test_a_wrong_signed_taken_is_refused_by_the_check(employee, annual_type, cycle):
    with pytest.raises(ValidationError) as raised:
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.TAKEN,
            quantity=Decimal("1.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 4, 1),
        )

    assert "sign_matches_type" in str(raised.value)


def test_a_wrong_signed_transaction_is_refused_at_the_database_too(employee, annual_type, cycle):
    """The CHECK itself, reached under ``full_clean()`` — a raw INSERT that
    skips model validation entirely, proving the trigger-independent guard is
    real at the database layer and not only enforced by the service function."""
    with tenant_context_of(employee), pytest.raises(IntegrityError) as raised, transaction.atomic():
        LeaveTransaction.objects.create(
            tenant_id=employee.tenant_id,
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_date=datetime.date(2026, 3, 31),
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("-1.250"),
            unit=LeaveCycle.Unit.DAYS,
        )

    assert "sign_matches_type" in str(raised.value)


def test_an_adjustment_needs_a_reason(employee, annual_type, cycle):
    with pytest.raises(ValidationError) as raised:
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.ADJUSTMENT,
            quantity=Decimal("1.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 4, 1),
            reason="",
        )

    assert "adjustment_states_a_reason" in str(raised.value)


def test_the_ledger_refuses_an_update_by_trigger(employee, annual_type, cycle):
    txn = post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("1.250"),
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=datetime.date(2026, 3, 31),
    )

    with tenant_context_of(txn), pytest.raises(DatabaseError) as raised, transaction.atomic():
        LeaveTransaction.objects.filter(pk=txn.pk).update(quantity=Decimal("9.000"))

    assert "append-only" in str(raised.value).lower()


def test_the_ledger_refuses_a_delete_by_trigger(employee, annual_type, cycle):
    txn = post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("1.250"),
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=datetime.date(2026, 3, 31),
    )

    with tenant_context_of(txn), pytest.raises(DatabaseError) as raised, transaction.atomic():
        LeaveTransaction.objects.filter(pk=txn.pk).delete()

    assert "append-only" in str(raised.value).lower()


def test_a_reversal_is_the_exact_opposite_and_restores_the_balance(employee, annual_type, cycle):
    from leave.balances import recompute_cycle

    original = post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("1.250"),
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=datetime.date(2026, 3, 31),
    )
    before = recompute_cycle(cycle).balance_quantity

    reversal = reverse_transaction(original, reason="Captured against the wrong cycle")

    assert reversal.transaction_type == TransactionType.REVERSAL
    assert reversal.quantity == -original.quantity
    assert reversal.reverses_transaction_id == original.pk

    after = recompute_cycle(cycle).balance_quantity
    assert after == before - original.quantity


def test_a_reversal_of_a_reversal_is_refused(employee, annual_type, cycle):
    original = post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("1.250"),
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=datetime.date(2026, 3, 31),
    )
    reversal = reverse_transaction(original, reason="Wrong cycle")

    with pytest.raises(LedgerRefusedError) as raised:
        reverse_transaction(reversal, reason="Undo the undo")

    assert "reversal of a reversal is refused" in str(raised.value)


def test_a_reversal_names_what_it_reverses(employee, annual_type, cycle):
    with tenant_context_of(employee), pytest.raises(IntegrityError) as raised, transaction.atomic():
        LeaveTransaction.objects.create(
            tenant_id=employee.tenant_id,
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_date=datetime.date(2026, 4, 1),
            transaction_type=TransactionType.REVERSAL,
            quantity=Decimal("1.000"),
            unit=LeaveCycle.Unit.DAYS,
            reverses_transaction=None,
        )

    assert "reversal_names_what_it_reverses" in str(raised.value)
