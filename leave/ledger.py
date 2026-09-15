"""Posting to leave_transaction — the one and only way a leave balance moves.

Nothing here computes a balance. This module writes rows; ``leave/balances.py``
derives the number from them. Keeping the two apart is what makes "derived,
never stored as an editable number" (invariant 3) true rather than aspirational.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db import transaction

from core.managers import tenant_context_of
from leave.models import LeaveCycle, LeaveTransaction, LeaveType

TransactionType = LeaveTransaction.TransactionType


class LedgerRefusedError(Exception):
    """The ledger may not be written this way. Nothing was written."""


def post_transaction(
    *,
    employee,
    leave_cycle: LeaveCycle,
    leave_type: LeaveType,
    transaction_type: str,
    quantity: Decimal,
    unit: str,
    transaction_date: datetime.date,
    calculation_basis: str = "",
    reason: str = "",
    created_by=None,
    leave_application_id_ref: int | None = None,
    payroll_run_id_ref: int | None = None,
) -> LeaveTransaction:
    """Write one ledger row. The sign convention (stated on the model) is
    enforced by the database CHECK, not repeated here — a caller that gets
    the sign wrong is refused at ``full_clean()``, naming the mismatch.
    """
    with transaction.atomic(), tenant_context_of(employee):
        txn = LeaveTransaction(
            tenant=employee.tenant,
            employee=employee,
            leave_cycle=leave_cycle,
            leave_type=leave_type,
            transaction_date=transaction_date,
            transaction_type=transaction_type,
            quantity=quantity,
            unit=unit,
            calculation_basis=calculation_basis,
            reason=reason,
            created_by_user=created_by,
            leave_application_id_ref=leave_application_id_ref,
            payroll_run_id_ref=payroll_run_id_ref,
        )
        txn.full_clean()
        txn.save()
    return txn


def reverse_transaction(
    original: LeaveTransaction, *, reason: str, created_by=None, transaction_date=None
) -> LeaveTransaction:
    """Correct ``original`` with its exact opposite — never a delete, never
    an edit (invariant 4).

    Refuses to reverse a reversal: the reversal IS the correction, and
    reversing it again would either restore the very error it corrected or
    require yet another special case to decide which one just happened.
    Correcting a REVERSAL that was itself wrong is a fresh, ordinary
    transaction (an adjustment, with its own reason), not a third link in a
    chain nobody could read back afterwards.
    """
    if original.transaction_type == TransactionType.REVERSAL:
        raise LedgerRefusedError(
            f"Transaction {original.pk} is itself a reversal of transaction "
            f"{original.reverses_transaction_id}. A reversal of a reversal is "
            f"refused — the reversal IS the correction, and there is nothing "
            f"further to undo. Post a fresh adjustment instead, with its own reason."
        )

    with transaction.atomic(), tenant_context_of(original):
        txn = LeaveTransaction(
            tenant_id=original.tenant_id,
            employee_id=original.employee_id,
            leave_cycle_id=original.leave_cycle_id,
            leave_type_id=original.leave_type_id,
            transaction_date=transaction_date or original.transaction_date,
            transaction_type=TransactionType.REVERSAL,
            quantity=-original.quantity,
            unit=original.unit,
            reverses_transaction=original,
            reason=reason,
            created_by_user=created_by,
        )
        txn.full_clean()
        txn.save()
    return txn
