"""The monthly accrual engine — P6 chunk 1's fourth task.

**Monthly and idempotent per employee, per leave type, per period.**
``leave_accrual_run``'s own ``UNIQUE (employer, leave_type, accrual_as_at)``
is what makes a second run write nothing (task 4) — a database constraint,
not merely a status check the engine remembers to make. ``run_monthly_accrual``
also checks for an existing COMPLETED run first, so the ordinary case never
even reaches the constraint.

**Scoped to ANNUAL leave for this chunk.** BCEA s20(2)'s three accrual
methods (plus the upfront-grant form ``employee_leave_entitlement`` already
supports) are fully specified by ``leave_rule_set`` today, and are
implemented here in full: straight-line monthly (day-based, by the
employee's own 5-day/6-day schedule), per-days-worked and per-hours-worked
(both attendance-based — this is why P5 had to come before P6), and a single
upfront grant of the whole cycle.

**SICK's BCEA s22 first-six-months rule is NOT implemented here, and that is
a deliberate, recorded gap, not an oversight.** It genuinely does accrue off
attendance the same way the per-17-hours method does — but "the first six
months" is a statutory THRESHOLD with no home yet in ``leave_rule_set``
(only the accrual RATIO, ``sick_leave_first_six_months_ratio``, is stored;
the six itself is not), and what happens to sick leave crediting AFTER that
window — a lump sum at eligibility, never smeared over 36 months the way
annual leave is — is a separate, undesigned mechanism this chunk was not
asked to build. Calling this engine for SICK, or any other ``accrues=True``
type besides ANNUAL, raises ``AccrualNotSupportedError`` naming the gap
rather than silently doing nothing — "resolve raises when a figure is
missing" applied to the THRESHOLD a rule would need, not only to a rate.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

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
    return worked / Decimal(rules.annual_accrual_ratio_days_worked), "per_17_days"


def _per_hours_worked_quantity(employee, rules, *, period_start, as_at) -> tuple[Decimal, str]:
    worked = _attendance_hours_worked(employee, period_start=period_start, period_end=as_at)
    return worked / Decimal(rules.annual_accrual_ratio_hours_worked), "per_17_hours"


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
    if leave_type.code != LeaveType.Code.ANNUAL:
        raise AccrualNotSupportedError(
            f"{leave_type.code} accrual is not implemented by this engine yet. "
            f"See leave/accrual.py's module docstring and O-22 in docs/DECISIONS.md "
            f"for exactly what is missing and why it is not guessed at."
        )

    ensure_cycles(employee, leave_type, horizon=as_at)
    cycle = current_cycle(employee, leave_type, as_at)
    if cycle is None:
        return None

    period_start = as_at.replace(day=1)
    already_this_period = LeaveTransaction.objects.filter(
        leave_cycle=cycle,
        transaction_type=LeaveTransaction.TransactionType.ACCRUAL,
        transaction_date__gte=period_start,
        transaction_date__lte=as_at,
    ).exists()
    if already_this_period:
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
        already_granted = LeaveTransaction.objects.filter(
            leave_cycle=cycle, transaction_type=LeaveTransaction.TransactionType.ACCRUAL
        ).exists()
        if already_granted:
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
