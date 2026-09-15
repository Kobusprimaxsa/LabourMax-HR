"""The forfeiture deadline warning — P6 chunk 3, task 3.

**Because nothing forfeits automatically (chunk 1's decision, chunk 3's own
manual capture in ``leave/forfeiture.py``), the employer carries the BCEA
s20(4) duty and this codebase carries the duty to make the deadline
impossible to miss.** This module answers one question, read-only: for an
employer, which employees have an ANNUAL leave cycle that has ENDED with a
balance still outstanding, how many days remain before the rule set's own
forfeiture window closes, and how urgent is that.

Nothing here writes anything — not a cycle, not a transaction, not a status
flag. A query, not a screen and not a job: the screen that renders this is
future work, and calling this from a scheduled task would be exactly the
automatic-forfeiture path chunk 1 and chunk 3 both refuse to build. It
returns data; a human decides what to do with it, including, eventually,
calling ``leave/forfeiture.py::capture_forfeiture()`` by hand.

**Scoped to ANNUAL only**, the same scope chunk 1's accrual engine already
settled on: BCEA s20 is where the forfeit-within-N-months grace period lives
(``leave_rule_set.annual_leave_forfeit_months``), and no other leave type in
this catalogue carries an equivalent cited figure — sick leave lapses at
cycle end outright (no grace window), and the rest are calendar-bound
entitlements a monthly cycle does not describe.

**Only OPEN cycles.** A cycle a terminated engagement has closed
(``employees.engagements.terminate()`` -> ``leave/cycles.py::close_cycles_at_termination()``)
is a termination-payout question under BCEA s40(b), which supersedes
forfeiture entirely — it is not this list's business, and including it would
conflate two different statutory duties behind one number.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.utils import timezone

from core.managers import tenant_context
from leave.balances import is_actually_stale, recompute_cycle
from leave.models import LeaveCycle, LeaveType
from statutory import resolve

#: How close to the statutory deadline a cycle must be before it reads as
#: DUE rather than APPROACHING. NOT a statutory figure — BCEA s20 gives the
#: SIX MONTHS itself (read from leave_rule_set below) and says nothing about
#: how urgently a screen should escalate inside that window. A reporting
#: judgement call, flagged for the labour law review (O-06) alongside this
#: chunk's other modelling choices, not a rate invented in place of a
#: citation — nothing it decides is a payment, an entitlement or a deadline.
DUE_WINDOW_DAYS = 30


class Bucket:
    """The three escalation buckets a rendering screen can key colour or
    ordering off. Plain string constants, not a TextChoices — this is a
    query result shape, not a column any table stores."""

    APPROACHING = "approaching"
    DUE = "due"
    PAST = "past"


@dataclass(frozen=True)
class ForfeitureWarning:
    """One employee, one ended cycle, one still-outstanding balance."""

    employee_id: int
    cycle: LeaveCycle
    balance: Decimal
    unit: str
    cycle_end: datetime.date
    forfeit_deadline: datetime.date
    days_remaining: int
    bucket: str


def _bucket_for(days_remaining: int) -> str:
    if days_remaining < 0:
        return Bucket.PAST
    if days_remaining <= DUE_WINDOW_DAYS:
        return Bucket.DUE
    return Bucket.APPROACHING


def forfeiture_warnings(employer, *, as_at: datetime.date | None = None) -> list[ForfeitureWarning]:
    """Every ended, still-open, still-outstanding ANNUAL cycle for this
    employer's employees, bucketed by proximity to its own forfeiture
    deadline. Ordered most urgent first.

    Recomputes a stale cycle before reading its balance — this list must
    never read a cached figure that has drifted from the ledger.
    """
    if as_at is None:
        as_at = timezone.localdate()

    results: list[ForfeitureWarning] = []

    with tenant_context(employer.tenant_id):
        cycles = LeaveCycle.objects.filter(
            employee__employer=employer,
            leave_type__code=LeaveType.Code.ANNUAL,
            status=LeaveCycle.Status.OPEN,
            cycle_end__lte=as_at,
        ).select_related("employee", "leave_type")

        for cycle in cycles:
            if cycle.is_stale or is_actually_stale(cycle):
                cycle = recompute_cycle(cycle)
            if cycle.balance_quantity <= 0:
                continue

            rules = resolve.leave_rules(cycle.employee.employer.sector, cycle.cycle_end)
            deadline = cycle.cycle_end + relativedelta(months=rules.annual_leave_forfeit_months)
            days_remaining = (deadline - as_at).days

            results.append(
                ForfeitureWarning(
                    employee_id=cycle.employee_id,
                    cycle=cycle,
                    balance=cycle.balance_quantity,
                    unit=cycle.unit,
                    cycle_end=cycle.cycle_end,
                    forfeit_deadline=deadline,
                    days_remaining=days_remaining,
                    bucket=_bucket_for(days_remaining),
                )
            )

    results.sort(key=lambda w: w.days_remaining)
    return results
