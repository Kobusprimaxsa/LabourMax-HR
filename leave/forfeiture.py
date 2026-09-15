"""Forfeiture capture — P6 chunk 3, task 2. Manual, by the employer, and
NOTHING else.

**The whole mechanism, and it is deliberately this small.** The employer
names an employee, a leave type and a cycle, states a quantity and a
reason, and this writes ONE negative ``forfeiture`` transaction to the
ledger, attributed to the user who captured it. No job, no scheduled task
and no engine path may call this — chunk 1's own negative test,
``test_no_forfeiture_transaction_is_ever_written_automatically``, is the
guard that catches a regression the moment anything tries.

**Why manual, in Kobus's own words (chunk 1's brief, restated here because
this is where it is finally exercised):** it keeps the BCEA s20(4) duty to
GRANT leave where the Act actually puts it — with the employer, not with a
script — and it gives the employer the chance to fix the leave plan instead
of a balance silently vanishing on a date nobody watched.

**Refuses a forfeiture larger than the cycle's balance, naming both
figures, and refuses one against a cycle with no balance at all.** The
cycle is recomputed from the ledger first — never trusting a cache that
might be stale — so the check is against the true current balance, not
whatever the last write happened to leave behind.

**Reversible like anything else in the ledger.** A forfeiture captured
against the wrong cycle, or for the wrong amount, is corrected with
``leave/ledger.py::reverse_transaction()`` — never edited, never deleted
(invariant 4). There is nothing forfeiture-specific about reversing one.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.managers import tenant_context_of
from leave.balances import recompute_cycle
from leave.ledger import post_transaction
from leave.models import LeaveCycle, LeaveTransaction


class ForfeitureRefusedError(Exception):
    """The forfeiture may not be captured this way. Nothing was written."""


def capture_forfeiture(
    cycle: LeaveCycle,
    *,
    quantity: Decimal,
    reason: str,
    captured_by,
) -> LeaveTransaction:
    """Capture one forfeiture against ``cycle``. Atomic.

    ``quantity`` is POSITIVE — the amount being taken away — and is stored
    on the ledger as the negative ``forfeiture`` transaction the sign
    convention requires; the caller never has to remember to negate it.
    """
    if not reason:
        raise ForfeitureRefusedError(
            "A forfeiture needs a reason — it is captured by hand precisely so "
            "there is a human explanation on the record, not a silent expiry."
        )
    if quantity <= 0:
        raise ForfeitureRefusedError(
            f"A forfeiture must be a positive quantity; {quantity} was given."
        )

    with transaction.atomic(), tenant_context_of(cycle):
        current = recompute_cycle(cycle)

        if current.balance_quantity <= 0:
            raise ForfeitureRefusedError(
                f"{current.employee}'s {current.leave_type.code} cycle "
                f"{current.cycle_number} has no balance to forfeit "
                f"({current.balance_quantity} {current.unit})."
            )
        if quantity > current.balance_quantity:
            raise ForfeitureRefusedError(
                f"Cannot forfeit {quantity} {current.unit} — the cycle's balance "
                f"is only {current.balance_quantity} {current.unit}. Forfeiting "
                f"more than the balance holds is not a correction, it is inventing "
                f"a debt the employee never had."
            )

        txn = post_transaction(
            employee=current.employee,
            leave_cycle=current,
            leave_type=current.leave_type,
            transaction_type=LeaveTransaction.TransactionType.FORFEITURE,
            quantity=-quantity,
            unit=current.unit,
            transaction_date=timezone.localdate(),
            calculation_basis="manual",
            reason=reason,
            created_by=captured_by,
        )

    return txn
