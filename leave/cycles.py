"""Generating leave cycles — lazily, idempotently, anchored to the engagement.

**Anchored to the CURRENT engagement's start date** (D-B). Cycle 1 starts on
``employees.engagements.current_engagement(employee).start_date``, and every
cycle after it starts exactly where the previous one ends. A re-hire is a
NEW engagement row (D-103), so it gets a fresh cycle 1 of its own — never a
continuation of the old engagement's numbering, and never its balance. The
old engagement's own BALANCE columns are never touched: whatever they held
when that engagement ended stays exactly as it was. Its cycle's DATE RANGE
is truncated to end at the termination (and its status set CLOSED) the next
time ``ensure_cycles`` runs for this employee and type, purely so a re-hire
starting inside that now-past window does not calendar-overlap it — see
``_close_cycles_from_other_engagements``.

**Generated lazily, up to a horizon, and idempotent.** Calling this twice for
the same employee, leave type and horizon creates nothing the second time —
the existing cycle numbers for THIS engagement are read first, and only the
gap between them and the horizon is filled in.

Only for leave types with their own balance (``balance_source == "own"``).
A type that draws on a parent (``ANNUAL_UNAUTHORISED``) or has no balance at
all (``UNPAID``) has nothing of its own for a cycle to describe.

**A ``balance_source == "parent"`` type resolves to its parent, transparently,
everywhere a cycle or an entitlement method is looked up** (P6 chunk 4, task
4 — unresolved since chunk 2's own flag, D-174). ``resolve_balance_leave_type()``
is the one place this happens; ``ensure_cycles()``, ``current_cycle()`` and
``accrual_method_for()`` all call it first, so a caller asking about
``ANNUAL_UNAUTHORISED`` transparently gets ``ANNUAL``'s own cycle, balance and
accrual method. The sub-type is never silenced, though — a ``leave_transaction``
still records its OWN ``leave_type`` (the label, "this was unauthorised"), only
``leave_cycle`` points at the parent's row, which is exactly the split
``leave/balances.py::recompute_cycle()`` already relies on (it sums by
``leave_cycle``, never by ``leave_type``), so no change was needed there at all.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db import models, transaction
from django.utils import timezone

from core.managers import tenant_context_of
from employees.engagements import current_engagement
from employees.models import Employee, EmployeeEngagement, EmployeeLeaveEntitlement
from leave.models import LeaveCycle, LeaveType

AccrualMethod = EmployeeLeaveEntitlement.AccrualMethod


class EntitlementNotResolvableError(Exception):
    """A cycle's entitlement figure needs a fact this employee does not carry yet."""


#: Statutory day figures resolvable straight from the rule set, by code. Not
#: every type has one — MATERNITY, PARENTAL and ADOPTION are calendar-bound
#: rather than day-banks (see leave/types.py), and STUDY/COMPASSIONATE have
#: no statutory figure to read at all. Absent here means "employer-set only".
_RESOLVABLE_CODES = frozenset(
    {LeaveType.Code.ANNUAL, LeaveType.Code.SICK, LeaveType.Code.FAMILY_RESPONSIBILITY}
)


def resolve_balance_leave_type(leave_type: LeaveType) -> LeaveType:
    """A ``balance_source == 'parent'`` type resolves to the parent it draws
    on, for every cycle, balance and accrual-method purpose (task 4, D-180).

    ``ANNUAL_UNAUTHORISED`` never gets a cycle of its own —
    ``leave_type.balance_source`` says so, and always has — but nothing
    before this resolved a query about it to the row that DOES carry a
    balance, so it silently read as an employee with nothing to lose. This
    function is the one place that resolution happens; every caller below
    that accepts a caller-supplied ``leave_type`` calls it first.

    Touching ``leave_type.parent_leave_type`` with no tenant pinned is safe
    here specifically because ``LeaveType`` is ``TenantSharedModel`` (D-127)
    and both rows in a parent/sub-type pair are shared system rows — a
    session with no tenant context still sees the shared catalogue, per
    ``TenantSharedManager``'s own read rule. This would NOT be safe for a
    tenant-scoped model's own FK.
    """
    is_parent_type = leave_type.balance_source == LeaveType.BalanceSource.PARENT
    if is_parent_type and leave_type.parent_leave_type_id:
        return leave_type.parent_leave_type
    return leave_type


def current_entitlement(
    employee: Employee, leave_type: LeaveType, on_date: datetime.date
) -> EmployeeLeaveEntitlement | None:
    """The effective-dated entitlement override in force on a date, if any.

    Absence is the ordinary case (D-127's own leave_type docstring): most
    employees have no row here, and that means the sectoral rule set applies
    untouched.

    Tenant-scoped, like everything below it on this page down to
    ``entitlement_quantity_for``. None of them pin their own context —
    the caller must already be inside ``tenant_context_of(employee)`` (or
    equivalent) before calling any of them, the same way
    ``attendance/capture.py::capture()`` opens one context and everything it
    calls runs inside it. Opening a fresh one per helper is how a lazy FK
    touch three calls deep silently returns nothing on a row already in hand.
    """
    return (
        EmployeeLeaveEntitlement.objects.filter(
            employee=employee, leave_type=leave_type, effective_from__lte=on_date
        )
        .filter(models.Q(effective_to__isnull=True) | models.Q(effective_to__gt=on_date))
        .order_by("-effective_from")
        .first()
    )


def accrual_method_for(
    employee: Employee, leave_type: LeaveType, on_date: datetime.date
) -> tuple[str, EmployeeLeaveEntitlement | None]:
    """The accrual method in force for this employee, type and date (D-A).

    BCEA s20(2) offers three methods and two of them require AGREEMENT — so
    the rule set's own (monthly, day-based) method applies UNLESS
    ``employee_leave_entitlement.accrual_method`` says otherwise. That row
    IS the recorded agreement; there is no employer-wide switch, because the
    Act wants the method agreed per employee, not decreed for all of them.

    Resolves a parent-balance type first (task 4) — an accrual-method
    agreement is inherently about the type that actually accrues (ANNUAL),
    never about a sub-type like ANNUAL_UNAUTHORISED that only ever labels a
    reason against it.
    """
    leave_type = resolve_balance_leave_type(leave_type)
    entitlement = current_entitlement(employee, leave_type, on_date)
    method = entitlement.accrual_method if entitlement else AccrualMethod.MONTHLY
    return method, entitlement


def unit_for_method(method: str) -> str:
    """DAYS for every method except PER_HOURS_WORKED (D-C)."""
    return (
        LeaveCycle.Unit.HOURS if method == AccrualMethod.PER_HOURS_WORKED else LeaveCycle.Unit.DAYS
    )


def _statutory_entitlement_quantity(
    employee: Employee, leave_type: LeaveType, *, on_date: datetime.date
) -> Decimal | None:
    """The base statutory entitlement for one cycle, in DAYS, or None if this
    leave type has no rule-set figure to read (see ``_RESOLVABLE_CODES``).
    """
    from attendance import scheduling
    from statutory import resolve

    if leave_type.code not in _RESOLVABLE_CODES:
        return None

    employer_sector = employee.employer.sector
    rules = resolve.leave_rules(employer_sector, on_date)

    schedule = scheduling.current_schedule(employee, on_date)
    six_day_week = schedule is not None and schedule.days_per_week > 5

    if leave_type.code == LeaveType.Code.ANNUAL:
        return (
            rules.annual_leave_days_per_cycle_6day
            if six_day_week
            else rules.annual_leave_days_per_cycle_5day
        )
    if leave_type.code == LeaveType.Code.SICK:
        # No literal fallback here (test_no_hardcoded_rates would flag one, and
        # rightly — a guessed days-per-week is exactly the kind of number this
        # codebase refuses to invent). An employee with no captured schedule
        # cannot have a sick entitlement computed at all; the caller needs a
        # schedule captured first, the same requirement P4 already puts on
        # every other pay-affecting calculation.
        if schedule is None:
            raise EntitlementNotResolvableError(
                f"{employee} has no work schedule captured as at {on_date:%d %B %Y}, "
                f"so sick leave's weeks-to-days conversion cannot be computed. "
                f"Capture a work schedule before generating this cycle."
            )
        return rules.sick_leave_weeks_equivalent * schedule.days_per_week
    if leave_type.code == LeaveType.Code.FAMILY_RESPONSIBILITY:
        return Decimal(rules.family_responsibility_days)
    raise AssertionError(f"Unhandled resolvable code: {leave_type.code}")  # pragma: no cover


def entitlement_quantity_for(
    employee: Employee,
    leave_type: LeaveType,
    entitlement: EmployeeLeaveEntitlement | None,
    *,
    on_date: datetime.date,
) -> Decimal:
    """The full-cycle entitlement, statutory plus contractual (sheet 02's own
    description of the column) — additive by default, a flat override when
    ``entitlement.replaces_statutory`` says so, matching how
    ``employee_leave_entitlement`` already works everywhere else it is read.
    """
    if entitlement is not None and entitlement.replaces_statutory:
        return entitlement.total_days_per_cycle_override

    base = _statutory_entitlement_quantity(employee, leave_type, on_date=on_date) or Decimal(0)
    additional = entitlement.additional_days_per_cycle if entitlement is not None else Decimal(0)
    return base + additional


def _close_cycle(cycle: LeaveCycle, *, termination_date: datetime.date) -> None:
    """Truncate one cycle's date range to end the day after termination, and
    mark it CLOSED. Never touches a balance column — only the date range and
    status, so whatever the cycle held stays exactly as it was (D-B).
    """
    new_end = termination_date + datetime.timedelta(days=1)
    if new_end < cycle.cycle_start:
        new_end = cycle.cycle_start
    if new_end < cycle.cycle_end:
        cycle.cycle_end = new_end
    cycle.status = LeaveCycle.Status.CLOSED
    cycle.closed_at = timezone.now()
    cycle.save(update_fields=["cycle_end", "status", "closed_at", "updated_at"])


def close_cycles_at_termination(engagement: EmployeeEngagement) -> list[LeaveCycle]:
    """Close every one of THIS engagement's own open leave cycles, right
    where the event happens (D-172, task 5).

    Chunk 1 only ever closed a prior engagement's cycle LAZILY, the next
    time ``ensure_cycles`` ran for a re-hire — which meant the boundary
    between "this engagement's service" and "closed" moved to whenever
    somebody next happened to look, exactly the D-132 lesson CLAUDE.md
    already states about cached and boundary-dependent facts: a value that
    is only ever recomputed on demand is wrong for however long nothing asks.
    Call this from ``employees.engagements.terminate()`` instead, so the
    cycle closes the moment service actually ends.

    ``ensure_cycles``'s own lazy call (now via this same function, for OTHER
    engagements) stays as the BACKSTOP, not the mechanism — for any cycle
    that predates this function existing, or a termination recorded through
    a path that does not yet call it.
    """
    if engagement.termination_date is None:
        return []

    closed: list[LeaveCycle] = []
    with transaction.atomic(), tenant_context_of(engagement):
        open_cycles = LeaveCycle.objects.filter(
            engagement=engagement, status=LeaveCycle.Status.OPEN
        )
        for cycle in open_cycles:
            _close_cycle(cycle, termination_date=engagement.termination_date)
            closed.append(cycle)
    return closed


def _close_cycles_from_other_engagements(
    employee: Employee, leave_type: LeaveType, current: EmployeeEngagement
) -> None:
    """THE BACKSTOP, not the mechanism (task 5). ``terminate()`` now closes
    an engagement's own cycles the moment service ends
    (``close_cycles_at_termination``) — this lazy path only still matters for
    a cycle that predates that call, or a termination recorded by a path
    that does not go through ``employees.engagements.terminate()``.

    Decision B's own docstring on ``LeaveCycle`` says a re-hire's old cycles
    "close at termination and are never revived" — the EXCLUDE constraint is
    scoped to ``(employee, leave_type)`` exactly as task 2 asks ("cycles
    never overlap for one employee and type"), not to the engagement — so a
    prior engagement's cycle, left open, still calendar-overlaps a re-hire
    starting inside its nominal window, and the constraint correctly refuses
    it. Only ever touches a cycle whose owning engagement has ALREADY
    terminated — ``engage()`` itself refuses a second open engagement, so by
    the time this runs, every OTHER engagement of this employee necessarily
    has a ``termination_date``.
    """
    stale_cycles = LeaveCycle.objects.filter(
        employee=employee, leave_type=leave_type, status=LeaveCycle.Status.OPEN
    ).exclude(engagement=current)

    for stale in stale_cycles:
        termination_date = stale.engagement.termination_date
        if termination_date is None:
            continue  # pragma: no cover — engage() refuses this state; defensive only.
        _close_cycle(stale, termination_date=termination_date)


def ensure_cycles(
    employee: Employee, leave_type: LeaveType, *, horizon: datetime.date
) -> list[LeaveCycle]:
    """Create every cycle from the current engagement's start up to
    ``horizon`` that does not already exist. Returns what was created.

    Stops generating past the engagement's own termination date, if it has
    one — a terminated engagement's leave does not keep accruing cycles for
    a period of employment that ended.

    Resolves a parent-balance type first (task 4) — asking for
    ``ANNUAL_UNAUTHORISED``'s cycles transparently ensures ``ANNUAL``'s.
    """
    leave_type = resolve_balance_leave_type(leave_type)
    if leave_type.balance_source != LeaveType.BalanceSource.OWN:
        return []

    created: list[LeaveCycle] = []

    # ONE context, ONE transaction, for the whole fill — including the
    # engagement lookup itself, which is exactly the call this bug lived in
    # once before: `current_engagement()` is a tenant-scoped query, and
    # calling it before this block opened returned None on every row in the
    # table, silently, rather than raising — the same lazy-touch trap
    # CLAUDE.md names, just on the function's very first line instead of a
    # nested helper. Every tenant-scoped read below (the engagement lookup,
    # the entitlement lookup, the employer/sector touch inside
    # entitlement_quantity_for) runs inside this one context now.
    with transaction.atomic(), tenant_context_of(employee):
        engagement = current_engagement(employee)
        if engagement is None:
            return []

        _close_cycles_from_other_engagements(employee, leave_type, engagement)

        effective_horizon = horizon
        if engagement.termination_date is not None:
            effective_horizon = min(horizon, engagement.termination_date)

        existing_numbers = set(
            LeaveCycle.objects.filter(engagement=engagement, leave_type=leave_type).values_list(
                "cycle_number", flat=True
            )
        )

        cycle_number = 1
        cycle_start = engagement.start_date

        while cycle_start <= effective_horizon:
            cycle_end = cycle_start + relativedelta(months=leave_type.cycle_months)

            if cycle_number not in existing_numbers:
                method, entitlement = accrual_method_for(employee, leave_type, cycle_start)
                unit = unit_for_method(method)
                entitlement_quantity = entitlement_quantity_for(
                    employee, leave_type, entitlement, on_date=cycle_start
                )

                cycle = LeaveCycle.objects.create(
                    tenant_id=employee.tenant_id,
                    employee=employee,
                    engagement=engagement,
                    leave_type=leave_type,
                    cycle_number=cycle_number,
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    unit=unit,
                    entitlement_quantity=entitlement_quantity,
                )
                created.append(cycle)

            cycle_number += 1
            cycle_start = cycle_end

    return created


def current_cycle(
    employee: Employee, leave_type: LeaveType, on_date: datetime.date
) -> LeaveCycle | None:
    """The cycle covering a date, if one has been generated yet. Does not
    generate one — callers that need it to exist call ``ensure_cycles`` first.

    Resolves a parent-balance type first (task 4) — ``ANNUAL_UNAUTHORISED``
    has no cycle of its own; this returns ``ANNUAL``'s.
    """
    leave_type = resolve_balance_leave_type(leave_type)
    with tenant_context_of(employee):
        return LeaveCycle.objects.filter(
            employee=employee,
            leave_type=leave_type,
            cycle_start__lte=on_date,
            cycle_end__gt=on_date,
        ).first()
