"""leave_cycle's balance columns — a CACHE, and nothing more (invariant 3).

Every figure is recomputed from ``leave_transaction``, every time. There is
no incremental update path: an incrementally-maintained balance that has
drifted cannot be told apart from a correct one, so the only thing trusted
is a fresh rebuild from the ledger.

**Staleness, two layers, mirroring D-153 exactly:**

- the FAST PATH — ``leave/staleness.py``'s signal sets ``is_stale`` the
  moment a ``leave_transaction`` against this cycle is saved. A signal
  rather than a check inside ``leave/ledger.py``, so the accrual engine and
  any future writer are covered by construction rather than by a call site
  somebody has to remember to add.
- the GROUND TRUTH — ``is_actually_stale()`` below, which never looks at the
  flag at all: it compares ``computed_at`` against the covered transactions'
  own ``created_at``, straight from the data. A flag that was missed, or
  cleared by hand, is still detectable this way.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.managers import tenant_context_of
from leave.models import LeaveCycle, LeaveTransaction

ZERO = Decimal("0")

TransactionType = LeaveTransaction.TransactionType

#: Which quantity column each transaction type feeds. ADJUSTMENT and
#: REVERSAL both land in adjusted_quantity — a reversal IS a correction, the
#: same shape as a manual adjustment, just one the ledger produced itself
#: rather than a person typing a number in.
_COLUMN_FOR_TYPE = {
    TransactionType.ACCRUAL: "accrued_quantity",
    TransactionType.OPENING_BALANCE: "carried_in_quantity",
    TransactionType.TAKEN: "taken_quantity",
    TransactionType.PAYOUT: "paid_out_quantity",
    TransactionType.FORFEITURE: "forfeited_quantity",
    TransactionType.ADJUSTMENT: "adjusted_quantity",
    TransactionType.REVERSAL: "adjusted_quantity",
}


def recompute_cycle(cycle: LeaveCycle) -> LeaveCycle:
    """Rebuild one cycle's balance columns from its own transactions.

    Idempotent — calling this twice with no new transaction in between
    produces identical figures, because both runs read the same rows the
    same way.
    """
    with transaction.atomic(), tenant_context_of(cycle):
        rows = list(LeaveTransaction.objects.filter(leave_cycle=cycle))

        totals = dict.fromkeys(set(_COLUMN_FOR_TYPE.values()), ZERO)
        for row in rows:
            column = _COLUMN_FOR_TYPE[row.transaction_type]
            totals[column] += row.quantity

        balance = (
            totals["carried_in_quantity"]
            + totals["accrued_quantity"]
            + totals["adjusted_quantity"]
            + totals["taken_quantity"]
            + totals["paid_out_quantity"]
            + totals["forfeited_quantity"]
        )

        for column, value in totals.items():
            setattr(cycle, column, value)
        cycle.balance_quantity = balance
        cycle.computed_at = timezone.now()
        cycle.is_stale = False
        cycle.save(
            update_fields=[
                *totals.keys(),
                "balance_quantity",
                "computed_at",
                "is_stale",
                "updated_at",
            ]
        )
    return cycle


def is_actually_stale(cycle: LeaveCycle) -> bool:
    """The ground truth, independent of ``is_stale``: has any transaction
    against this cycle been written since it was last computed?
    """
    with tenant_context_of(cycle):
        latest = LeaveTransaction.objects.filter(leave_cycle=cycle).aggregate(
            latest=Max("created_at")
        )["latest"]
    if latest is None:
        return False
    return latest > cycle.computed_at


def balance_as_at(employee, leave_type, on_date) -> LeaveCycle | None:
    """The cycle covering a date, recomputed if it is stale by either
    measure. None if no cycle has been generated for that date yet.
    """
    from leave.cycles import current_cycle

    cycle = current_cycle(employee, leave_type, on_date)
    if cycle is None:
        return None
    if cycle.is_stale or is_actually_stale(cycle):
        cycle = recompute_cycle(cycle)
    return cycle
