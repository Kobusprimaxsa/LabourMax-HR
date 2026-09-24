"""The negative balance report — P6 chunk 4, task 1.

**D-176 was the right diagnosis and the wrong fix.** Chunk 3's property test
found that accrue, spend against it, then reverse the accrual is a REAL
sequence: an accrual posted in error, consumed, then corrected — and the
reversal alone can leave the balance negative, honestly, because the days
were actually taken and the entitlement that covered them was withdrawn
after the fact. The prior fix suppressed the property test's own coverage of
that sequence rather than accepting what it found, which removed the
evidence instead of closing the gap: in production the sequence still runs
and the balance still goes negative, silently.

**The false invariant this replaces**: "a balance is never negative except
where an overdrawn application made it so." That is not true. An overdrawn
application, a reversal of a spent accrual, and (P7) a termination payout
can all put a cycle below zero, legitimately. What IS true, and is asserted
instead — here, and in ``leave/tests/test_reconciliation_property.py`` — is
that the balance always equals the ledger's own sum exactly, and every
negative balance is fully attributable to identifiable rows: this module.

**Same shape as ``leave/warnings.py``'s forfeiture list, deliberately**: a
read-only query, not a screen and not a job. It writes nothing — not a
cycle, not a transaction, not a flag — and calling it from a scheduled task
would manufacture exactly the kind of automatic action this codebase has
refused to build at every other turn (forfeiture, sick certificates). A
human is shown the fact; nothing here decides what to do about it.

**NOT scoped to ANNUAL.** Unlike the forfeiture warning, a negative balance
is not a leave-type-specific statutory question — it can happen to any
leave type with its own ledger, so every OPEN cycle for the employer is
checked, across leave types.

**P7 must not net this off a termination payout** (recorded as a decision
against P7, not implemented here — there is no payout code in this
codebase yet). Recovering an outstanding negative leave balance from an
employee's final payment is a DEDUCTION, and BCEA s34 requires the
employee's written consent for it, exactly like any other deduction: it is
not something a payslip may silently subtract because the sign happens to
be convenient. P7's termination calculation must call this module, surface
the figure for a human decision, and never fold it into the payout amount
on its own authority.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from core.managers import tenant_context
from leave.balances import is_actually_stale, recompute_cycle
from leave.models import LeaveCycle, LeaveTransaction


@dataclass(frozen=True)
class NegativeBalance:
    """One employee, one cycle, currently below zero — and every ledger row
    that sums to that figure, since the balance is a plain linear sum of all
    of them and there is no principled way to single out a "responsible"
    subset.
    """

    employee_id: int
    cycle: LeaveCycle
    leave_type_code: str
    balance: Decimal
    unit: str
    causing_transactions: list[LeaveTransaction]


def negative_balances(employer, *, as_at: datetime.date | None = None) -> list[NegativeBalance]:
    """Every OPEN cycle, any leave type, currently carrying a balance below
    zero for this employer's employees. Ordered most negative first.

    Recomputes a stale cycle before reading its balance — this list must
    never report a cached figure that has drifted from the ledger, in
    either direction.
    """
    if as_at is None:
        as_at = timezone.localdate()

    results: list[NegativeBalance] = []

    with tenant_context(employer.tenant_id):
        cycles = LeaveCycle.objects.filter(
            employee__employer=employer,
            status=LeaveCycle.Status.OPEN,
        ).select_related("employee", "leave_type")

        for cycle in cycles:
            if cycle.is_stale or is_actually_stale(cycle):
                cycle = recompute_cycle(cycle)
            if cycle.balance_quantity >= 0:
                continue

            causing = list(
                LeaveTransaction.objects.filter(leave_cycle=cycle).order_by(
                    "transaction_date", "pk"
                )
            )

            results.append(
                NegativeBalance(
                    employee_id=cycle.employee_id,
                    cycle=cycle,
                    leave_type_code=cycle.leave_type.code,
                    balance=cycle.balance_quantity,
                    unit=cycle.unit,
                    causing_transactions=causing,
                )
            )

    results.sort(key=lambda n: n.balance)
    return results


def at_termination(engagement) -> list[NegativeBalance]:
    """Every cycle of ONE ended engagement, any leave type and any status,
    below zero — the figure a termination payout surfaces and never nets off
    (D-185, D-309).

    ``negative_balances()`` reads OPEN cycles only, and ``terminate()`` closes
    a leaver's cycles the moment service ends (D-172) — so for exactly the
    employee D-185 is about, the employer-wide list would say nothing. Same
    recompute-before-read rule; same nothing-written.
    """
    results: list[NegativeBalance] = []
    with tenant_context(engagement.tenant_id):
        cycles = LeaveCycle.objects.filter(engagement=engagement).select_related("leave_type")
        for cycle in cycles:
            if cycle.is_stale or is_actually_stale(cycle):
                cycle = recompute_cycle(cycle)
            if cycle.balance_quantity >= 0:
                continue
            results.append(
                NegativeBalance(
                    employee_id=cycle.employee_id,
                    cycle=cycle,
                    leave_type_code=cycle.leave_type.code,
                    balance=cycle.balance_quantity,
                    unit=cycle.unit,
                    causing_transactions=list(
                        LeaveTransaction.objects.filter(leave_cycle=cycle).order_by(
                            "transaction_date", "pk"
                        )
                    ),
                )
            )
    results.sort(key=lambda n: n.balance)
    return results
