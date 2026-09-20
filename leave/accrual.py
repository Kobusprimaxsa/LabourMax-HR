"""The monthly accrual engine — P6 chunk 1's fourth task, extended in chunk 4.

**Monthly and idempotent per employee, per leave type, per period.**
``leave_accrual_run``'s own ``UNIQUE (employer, leave_type, accrual_as_at)``
is what makes a second run write nothing (task 4) — a database constraint,
not merely a status check the engine remembers to make. ``run_monthly_accrual``
also checks for an existing COMPLETED run first, so the ordinary case never
even reaches the constraint. THREE leave types go through the SAME
``run_monthly_accrual`` entry point now — ANNUAL, SICK and
FAMILY_RESPONSIBILITY — dispatching on ``leave_type.code`` inside
``accrue_employee`` rather than each gaining a parallel run mechanism
(P6 chunk 4, task 2's own instruction).

**ANNUAL** — BCEA s20(2)'s three accrual methods (plus the upfront-grant
form ``employee_leave_entitlement`` already supports) are fully specified by
``leave_rule_set``: straight-line monthly (day-based, by the employee's own
5-day/6-day schedule), per-days-worked and per-hours-worked (both
attendance-based — this is why P5 had to come before P6), and a single
upfront grant of the whole cycle.

**SICK (P6 chunk 4, D-181)** — BCEA s22 is genuinely two-phase, and neither
phase is agreement-based the way ANNUAL's three methods are, so SICK never
goes through ``accrual_method_for()``/``AccrualMethod`` at all:

- Cycle 1 (the employee's first 36-month sick cycle) starts with the
  ATTENDANCE-DRIVEN ratio from s22(1) — one day per
  ``sick_leave_first_six_months_ratio`` days worked, from
  ``attendance_day.days_worked_equivalent``, monthly and idempotent exactly
  like ANNUAL's per-days-worked method. This is why P5 had to exist for SICK
  too, not only for ANNUAL's hourly method.
- At the ``SICK_LEAVE_FIRST_PERIOD_MONTHS`` mark (a new ``statutory_parameter``
  this chunk — the SIX itself was the gap chunk 1 flagged and never claimed
  to have closed), ONE top-up transaction transitions the cycle to the full
  six-week-equivalent entitlement, and NO further accrual happens for the
  rest of cycle 1 — the lump sum covers it, per s22(2)'s own "following sick
  leave cycle" wording.
- Cycle 2 onward never sees the ratio phase at all — by definition, a SECOND
  36-month sick cycle cannot fall inside the employee's first six months of
  employment — so it is granted the full six-week-equivalent UPFRONT, once,
  at cycle start.

There is ONE entitlement per sick cycle. s22(3)'s ratio is a restriction on
how much of it is AVAILABLE in the first six months, not a second
entitlement; at six months the rest becomes available (D-181, amended).
Whether sick leave already drawn in that time is then deducted is s22(4)'s
"may" — the employer's election, ``SICK_FIRST_CYCLE_REDUCTION`` (D-186),
resolved here and handed to the pure ``sick_first_cycle_top_up()``.

See ``_accrue_sick()`` for exactly how the transition preserves what was
already taken rather than doubling the entitlement or stranding it — the
two wrong readings task 2 named explicitly — and D-181 in
``docs/DECISIONS.md`` for the arithmetic proof.

**FAMILY_RESPONSIBILITY (P6 chunk 4)** — BCEA s27 is not agreement-based
either: the full cycle entitlement (``family_responsibility_days``) is
GRANTED once, upfront, at cycle start — never accrued monthly, never
carried to the next cycle (each cycle simply starts its own grant fresh),
and (``leave_type.payable_on_termination = False``, already seeded in
chunk 1) never paid out. s27(1)'s two eligibility limbs are enforced
(D-189, ``leave/eligibility.py``): nothing is granted until both hold, and
an application is refused naming whichever fails.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.managers import tenant_context
from employees.models import EmployeeLeaveEntitlement
from leave.cycles import accrual_method_for, current_cycle, ensure_cycles, unit_for_method
from leave.ledger import post_transaction
from leave.models import LeaveAccrualRun, LeaveTransaction, LeaveType

ZERO = Decimal("0")
AccrualMethod = EmployeeLeaveEntitlement.AccrualMethod

#: ``leave_transaction.days``/``.hours`` are ``DecimalField(decimal_places=3)``
#: (D-182, a hardening fix found while building this chunk's own ratio-based
#: SICK accrual). An un-quantized division — ``worked / ratio`` — carries
#: Python's full 28-significant-digit context precision, which
#: ``full_clean()``'s own ``DecimalValidator`` refuses outright the moment the
#: division is not exact. Chunk 1's ``_per_days_worked_quantity`` and
#: ``_per_hours_worked_quantity`` carried this same latent gap, untested
#: because every existing test happened to divide evenly; fixed here
#: alongside the new SICK ratio, in the same place, the same way
#: ``leave/applications.py`` already rounds a part-day's hours.
_QUANTUM = Decimal("0.001")

#: BCEA s22(1)-(2)'s "first six months of employment" — the duration itself,
#: as opposed to the accrual RATIO (``sick_leave_first_six_months_ratio``,
#: already in ``leave_rule_set``). Chunk 1's own accrual.py docstring named
#: this exact gap; see tools/build_sick_accrual_fixture.py and D-181.
SICK_FIRST_PERIOD_PARAMETER = "SICK_LEAVE_FIRST_PERIOD_MONTHS"

#: calculation_basis markers for the two SICK-specific transaction shapes,
#: read by tests and by anyone auditing a sick leave cycle's own history.
SICK_TRANSITION_BASIS = "sick_six_month_transition"
SICK_TRANSITION_UNREDUCED_BASIS = "sick_six_month_transition_unreduced"
SICK_FIRST_CYCLE_REDUCTION_SETTING = "SICK_FIRST_CYCLE_REDUCTION"
SICK_UPFRONT_BASIS = "sick_upfront_cycle"
FAMILY_RESPONSIBILITY_BASIS = "family_responsibility_upfront"


class AccrualRefusedError(Exception):
    """The engine cannot run this. Nothing was written."""


class AccrualNotSupportedError(AccrualRefusedError):
    """This leave type's accrual is not implemented by this engine yet."""


def _schedule_is_six_day(employee, on_date: datetime.date) -> bool:
    from attendance import scheduling

    schedule = scheduling.current_schedule(employee, on_date)
    return schedule is not None and schedule.days_per_week > 5


def _monthly_quantity(employee, rules, *, as_at: datetime.date) -> tuple[Decimal, str]:
    if _schedule_is_six_day(employee, as_at):
        return rules.annual_accrual_days_per_month_6day, "monthly_1_50"
    return rules.annual_accrual_days_per_month_5day, "monthly_1_25"


def _attendance_days_worked(employee, *, period_start, period_end) -> Decimal:
    from attendance.models import AttendanceDay

    total = AttendanceDay.objects.filter(
        employee=employee, work_date__gte=period_start, work_date__lte=period_end
    ).aggregate(total=Sum("days_worked_equivalent"))["total"]
    return total or ZERO


def _attendance_hours_worked(employee, *, period_start, period_end) -> Decimal:
    from attendance.models import AttendanceDay

    days = AttendanceDay.objects.filter(
        employee=employee, work_date__gte=period_start, work_date__lte=period_end
    )
    return sum(
        (
            d.ordinary_hours + d.overtime_hours + d.sunday_hours + d.public_holiday_hours
            for d in days
        ),
        ZERO,
    )


def _per_days_worked_quantity(employee, rules, *, period_start, as_at) -> tuple[Decimal, str]:
    worked = _attendance_days_worked(employee, period_start=period_start, period_end=as_at)
    quantity = worked / Decimal(rules.annual_accrual_ratio_days_worked)
    return quantity.quantize(_QUANTUM, rounding=ROUND_HALF_UP), "per_17_days"


def _per_hours_worked_quantity(employee, rules, *, period_start, as_at) -> tuple[Decimal, str]:
    worked = _attendance_hours_worked(employee, period_start=period_start, period_end=as_at)
    quantity = worked / Decimal(rules.annual_accrual_ratio_hours_worked)
    return quantity.quantize(_QUANTUM, rounding=ROUND_HALF_UP), "per_17_hours"


def _already_accrued_this_period(cycle, *, period_start, as_at) -> bool:
    return LeaveTransaction.objects.filter(
        leave_cycle=cycle,
        transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
        transaction_date__gte=period_start,
        transaction_date__lte=as_at,
    ).exists()


def _any_accrual_posted(cycle) -> bool:
    return LeaveTransaction.objects.filter(
        leave_cycle=cycle, transaction_type=LeaveTransaction.TransactionType.ACCRUAL
    ).exists()


def _accrue_annual(
    employee, leave_type: LeaveType, *, as_at: datetime.date
) -> LeaveTransaction | None:
    """BCEA s20(2)'s three agreement-based methods, plus the upfront grant —
    chunk 1's own logic, unchanged in substance, only moved into its own
    function so ``accrue_employee`` can dispatch by leave type.
    """
    ensure_cycles(employee, leave_type, horizon=as_at)
    cycle = current_cycle(employee, leave_type, as_at)
    if cycle is None:
        return None

    period_start = as_at.replace(day=1)
    if _already_accrued_this_period(cycle, period_start=period_start, as_at=as_at):
        return None

    from statutory import resolve

    rules = resolve.leave_rules(employee.employer.sector, as_at)
    method, _entitlement = accrual_method_for(employee, leave_type, as_at)

    if method == AccrualMethod.MONTHLY:
        quantity, basis = _monthly_quantity(employee, rules, as_at=as_at)
    elif method == AccrualMethod.PER_DAYS_WORKED:
        quantity, basis = _per_days_worked_quantity(
            employee, rules, period_start=period_start, as_at=as_at
        )
    elif method == AccrualMethod.PER_HOURS_WORKED:
        quantity, basis = _per_hours_worked_quantity(
            employee, rules, period_start=period_start, as_at=as_at
        )
    elif method == AccrualMethod.UPFRONT_ANNUAL:
        if _any_accrual_posted(cycle):
            return None
        quantity, basis = cycle.entitlement_quantity, "upfront_annual"
    else:
        raise AccrualRefusedError(f"Unknown accrual method: {method}")  # pragma: no cover

    if quantity <= 0:
        return None

    return post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=leave_type,
        transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
        quantity=quantity,
        unit=unit_for_method(method),
        transaction_date=as_at,
        calculation_basis=basis,
    )


def _accrue_sick(
    employee, leave_type: LeaveType, *, as_at: datetime.date
) -> LeaveTransaction | None:
    """BCEA s22, two-phase, D-181. Never goes through ``AccrualMethod`` —
    sick leave accrual is prescribed, not agreed.

    Cycle 1 only: attendance-driven ratio accrual up to the
    ``SICK_LEAVE_FIRST_PERIOD_MONTHS`` mark, then ONE top-up transaction to
    the full six-week-equivalent, then nothing further for the rest of the
    cycle. Cycle 2 onward: the full six-week-equivalent, upfront, once.
    """
    ensure_cycles(employee, leave_type, horizon=as_at)
    cycle = current_cycle(employee, leave_type, as_at)
    if cycle is None:
        return None

    if cycle.cycle_number > 1:
        # Never inside the employee's first six months of EMPLOYMENT by
        # construction — cycle 1 alone runs 36 months (D-181).
        if _any_accrual_posted(cycle):
            return None
        quantity = cycle.entitlement_quantity
        if quantity <= 0:
            return None
        return post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=leave_type,
            transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
            quantity=quantity,
            unit=cycle.unit,
            transaction_date=cycle.cycle_start,
            calculation_basis=SICK_UPFRONT_BASIS,
        )

    from statutory import resolve

    first_period_months = int(resolve.parameter_value(SICK_FIRST_PERIOD_PARAMETER, as_at))
    transition_date = cycle.cycle_start + relativedelta(months=first_period_months)

    if as_at < transition_date:
        period_start = as_at.replace(day=1)
        if _already_accrued_this_period(cycle, period_start=period_start, as_at=as_at):
            return None
        rules = resolve.leave_rules(employee.employer.sector, as_at)
        worked = _attendance_days_worked(employee, period_start=period_start, period_end=as_at)
        quantity = (worked / Decimal(rules.sick_leave_first_six_months_ratio)).quantize(
            _QUANTUM, rounding=ROUND_HALF_UP
        )
        if quantity <= 0:
            return None
        return post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=leave_type,
            transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
            quantity=quantity,
            unit=cycle.unit,
            transaction_date=as_at,
            calculation_basis="per_26_days_first_6m",
        )

    # At or past the transition: the one-time top-up, if not already done.
    already_transitioned = LeaveTransaction.objects.filter(
        leave_cycle=cycle,
        calculation_basis__in=[SICK_TRANSITION_BASIS, SICK_TRANSITION_UNREDUCED_BASIS],
    ).exists()
    if already_transitioned:
        return None

    from employers.onboarding import setting_value

    reduce_by_taken = bool(setting_value(employee.employer, SICK_FIRST_CYCLE_REDUCTION_SETTING))

    quantity = sick_first_cycle_top_up(
        entitlement=cycle.entitlement_quantity,
        accrued=_net_accrued(cycle),
        taken=_taken_before(cycle, transition_date),
        reduce_by_taken=reduce_by_taken,
    )
    # Deliberately does NOT reduce the balance if the ratio phase somehow
    # over-accrued beyond the full entitlement (quantity <= 0) — the ACCRUAL
    # sign CHECK cannot carry a negative quantity, and manufacturing an
    # ADJUSTMENT's reason for an automatic engine action would be inventing an
    # explanation nobody gave. Unreachable at the ratios this rule set loads.
    if quantity <= 0:
        return None

    if reduce_by_taken:
        basis = SICK_TRANSITION_BASIS
        reason = (
            "BCEA s22(3) availability ends: balance of the s22(2) entitlement, less "
            "sick leave already drawn (s22(4) exercised, SICK_FIRST_CYCLE_REDUCTION)"
        )
    else:
        basis = SICK_TRANSITION_UNREDUCED_BASIS
        # Kept inside leave_transaction.reason's 255 characters, which the first
        # attempt at this wording overran by eighty.
        reason = (
            "BCEA s22(3) availability ends: full s22(2) entitlement, drawn days NOT "
            "deducted (s22(4) not exercised, SICK_FIRST_CYCLE_REDUCTION). This employer "
            "gives more paid sick leave this cycle than the Act requires - lawful, the "
            "Act being a floor"
        )

    return post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=leave_type,
        transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
        quantity=quantity,
        unit=cycle.unit,
        transaction_date=transition_date,
        calculation_basis=basis,
        reason=reason,
    )


def _net_accrued(cycle) -> Decimal:
    """Every ACCRUAL row on this cycle, net of any reversal of one. An accrual
    posted in error and reversed was never accrued; summing ACCRUAL rows alone
    subtracted it from the six-month top-up anyway, and the employee reached
    six months short by exactly the reversed amount (D-190, found by the
    property generator)."""
    accrual_rows = LeaveTransaction.objects.filter(
        leave_cycle=cycle, transaction_type=LeaveTransaction.TransactionType.ACCRUAL
    )
    accrued = accrual_rows.aggregate(total=Sum(cycle.unit))["total"] or ZERO
    reversed_back = (
        LeaveTransaction.objects.filter(
            leave_cycle=cycle,
            transaction_type=LeaveTransaction.TransactionType.REVERSAL,
            reverses_transaction__in=accrual_rows,
        ).aggregate(total=Sum(cycle.unit))["total"]
        or ZERO
    )
    return accrued + reversed_back


def _taken_before(cycle, before: datetime.date) -> Decimal:
    """Sick leave drawn from this cycle before ``before``, as a POSITIVE
    quantity: every TAKEN row, net of any reversal of one (a cancelled
    application gives its days back, so they were never drawn)."""
    taken_rows = LeaveTransaction.objects.filter(
        leave_cycle=cycle,
        transaction_type=LeaveTransaction.TransactionType.TAKEN,
        transaction_date__lt=before,
    )
    drawn = taken_rows.aggregate(total=Sum(cycle.unit))["total"] or ZERO
    returned = (
        LeaveTransaction.objects.filter(
            leave_cycle=cycle,
            transaction_type=LeaveTransaction.TransactionType.REVERSAL,
            reverses_transaction__in=taken_rows,
        ).aggregate(total=Sum(cycle.unit))["total"]
        or ZERO
    )
    return -(drawn + returned)


def sick_first_cycle_top_up(
    *, entitlement: Decimal, accrued: Decimal, taken: Decimal, reduce_by_taken: bool
) -> Decimal:
    """The one top-up at the end of s22(3)'s first six months. Pure: no ORM, no
    settings — the election arrives as ``reduce_by_taken``, already resolved.

    There is ONE entitlement per cycle, E (s22(2)). s22(3) does not create a
    second one; it restricts how much of E is available during the first six
    months to the ratio amount A (``accrued``). Days drawn in that time, T
    (``taken``), are already in the ledger, so the balance going in is A - T.

    - ``reduce_by_taken`` (s22(4) exercised, the default): the rest of E
      becomes available, and what was drawn stays drawn. Top-up E - A; balance
      out (A - T) + (E - A) = E - T.
    - not exercised: the full E becomes available and T is not deducted, so the
      employee may draw E + T across the cycle. Top-up E - A + T; balance out E.
    """
    if reduce_by_taken:
        return entitlement - accrued
    return entitlement - accrued + taken


def _accrue_family_responsibility(
    employee, leave_type: LeaveType, *, as_at: datetime.date
) -> LeaveTransaction | None:
    """BCEA s27: granted once, upfront, at cycle start — never accrued
    monthly, never carried to the next cycle (each cycle grants its own
    fresh amount, with no reference to what a prior cycle held).

    BCEA s27(1) eligibility is enforced here too (D-189,
    ``leave/eligibility.py``): nothing is granted while either limb fails, so
    no balance is ever shown for leave the employee does not have. That is the
    ordinary state of every new hire, so it returns None rather than raising —
    the application layer is where a refusal names the limb. Once eligible the
    grant is dated the later of cycle start and the first eligible day, never
    backdated to before the leave existed.
    """
    from leave.eligibility import family_responsibility_eligibility

    ensure_cycles(employee, leave_type, horizon=as_at)
    cycle = current_cycle(employee, leave_type, as_at)
    if cycle is None:
        return None
    if _any_accrual_posted(cycle):
        return None

    eligibility = family_responsibility_eligibility(employee, as_at)
    if not eligibility.is_eligible:
        return None

    quantity = cycle.entitlement_quantity
    if quantity <= 0:
        return None
    granted_on = max(cycle.cycle_start, eligibility.eligible_from)

    return post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=leave_type,
        transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
        quantity=quantity,
        unit=cycle.unit,
        transaction_date=granted_on,
        calculation_basis=FAMILY_RESPONSIBILITY_BASIS,
    )


_DISPATCH = {
    LeaveType.Code.ANNUAL: _accrue_annual,
    LeaveType.Code.SICK: _accrue_sick,
    LeaveType.Code.FAMILY_RESPONSIBILITY: _accrue_family_responsibility,
}


def accrue_employee(
    employee, leave_type: LeaveType, *, as_at: datetime.date
) -> LeaveTransaction | None:
    """Post this employee's accrual for one leave type as at a date, or
    return None if there is nothing to post — the type does not accrue, the
    cycle already received this period's accrual, or the computed quantity
    is zero.

    Caller must already hold ``tenant_context_of(employee)`` (or
    ``tenant_context(employee.tenant_id)``) — this function, and everything
    it calls, assumes the context is pinned, the same discipline
    ``leave/cycles.py`` documents at its own top.
    """
    if not leave_type.accrues:
        return None

    handler = _DISPATCH.get(leave_type.code)
    if handler is None:
        raise AccrualNotSupportedError(
            f"{leave_type.code} accrual is not implemented by this engine yet. "
            f"See leave/accrual.py's module docstring and O-22 in docs/DECISIONS.md "
            f"for exactly what is missing and why it is not guessed at."
        )
    return handler(employee, leave_type, as_at=as_at)


def run_monthly_accrual(
    employer, leave_type: LeaveType, accrual_as_at: datetime.date
) -> LeaveAccrualRun:
    """Run one accrual pass for every one of ``employer``'s employees, for
    one leave type, as at one date. Idempotent: a second call with the same
    three arguments returns the existing COMPLETED run unchanged, and writes
    nothing further to the ledger.

    One context, one transaction, for the whole run — every employee
    processed shares it, which is also why a run either completes in full or
    fails in full rather than leaving half an employer's payroll accrued.
    """
    from employees.models import Employee

    with transaction.atomic(), tenant_context(employer.tenant_id):
        existing = LeaveAccrualRun.objects.filter(
            employer=employer,
            leave_type=leave_type,
            accrual_as_at=accrual_as_at,
            status=LeaveAccrualRun.Status.COMPLETED,
        ).first()
        if existing is not None:
            return existing

        run, _created = LeaveAccrualRun.objects.get_or_create(
            employer=employer,
            leave_type=leave_type,
            accrual_as_at=accrual_as_at,
            defaults={
                "tenant_id": employer.tenant_id,
                "status": LeaveAccrualRun.Status.RUNNING,
                "started_at": timezone.now(),
            },
        )
        if not _created and run.status == LeaveAccrualRun.Status.COMPLETED:
            return run

        employees_processed = 0
        transactions_created = 0
        for employee in Employee.objects.filter(employer=employer):
            employees_processed += 1
            txn = accrue_employee(employee, leave_type, as_at=accrual_as_at)
            if txn is not None:
                transactions_created += 1

        run.employees_processed = employees_processed
        run.transactions_created = transactions_created
        run.status = LeaveAccrualRun.Status.COMPLETED
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "employees_processed",
                "transactions_created",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    return run
