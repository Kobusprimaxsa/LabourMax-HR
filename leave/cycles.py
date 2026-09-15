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
    """
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


def _close_cycles_from_other_engagements(
    employee: Employee, leave_type: LeaveType, current: EmployeeEngagement
) -> None:
    """Truncate a PRIOR engagement's own open cycle to end at its
    termination, so a re-hire's fresh cycle 1 does not calendar-overlap it.

    Decision B's own docstring on ``LeaveCycle`` says a re-hire's old cycles
    "close at termination and are never revived" — but nothing made that
    true at the row level until now. The EXCLUDE constraint is scoped to
    ``(employee, leave_type)`` exactly as task 2 asks ("cycles never overlap
    for one employee and type"), not to the engagement — so a prior
    engagement's cycle, left at its full nominal 12 (or 36) months, still
    calendar-overlaps a re-hire that starts inside that window, and the
    constraint correctly refuses it. The fix is not to weaken the
    constraint; it is to make the prior cycle's own date range reflect what
    actually happened: it stopped being open the day service stopped.

    Only ever shortens a cycle whose owning engagement has ALREADY
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

        new_end = termination_date + datetime.timedelta(days=1)
        if new_end < stale.cycle_start:
            new_end = stale.cycle_start
        if new_end < stale.cycle_end:
            stale.cycle_end = new_end
        stale.status = LeaveCycle.Status.CLOSED
        stale.closed_at = timezone.now()
        stale.save(update_fields=["cycle_end", "status", "closed_at", "updated_at"])


def ensure_cycles(
    employee: Employee, leave_type: LeaveType, *, horizon: datetime.date
) -> list[LeaveCycle]:
    """Create every cycle from the current engagement's start up to
    ``horizon`` that does not already exist. Returns what was created.

    Stops generating past the engagement's own termination date, if it has
    one — a terminated engagement's leave does not keep accruing cycles for
    a period of employment that ended.
    """
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
    """
    with tenant_context_of(employee):
        return LeaveCycle.objects.filter(
            employee=employee,
            leave_type=leave_type,
            cycle_start__lte=on_date,
            cycle_end__gt=on_date,
        ).first()
