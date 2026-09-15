"""Submitting a leave application — P6 chunk 2, task 2.

**Evidence gates pay, not leave** (task 1's own rule, applied here). Nothing
in this module refuses an application for want of a certificate. BCEA s23
conditions the employer's right to WITHHOLD PAY on an unevidenced absence
beyond a threshold — it never conditions the right to TAKE the leave. A
sick application with no certificate, however long, is always created and
always reaches ``submitted``; only ``is_paid`` on its days is affected.

**An overdrawn application is never refused either** (Kobus's own framing).
The excess is capped against the ledger — never let a balance run negative
by paying what is not there — and the days beyond what the balance can
cover are marked ``is_paid=False`` and ``deducted_from_balance=False``:
they are taken, they are just unpaid. ``exceeds_balance`` and
``unpaid_days`` carry the audit; ``leave/authorisation.py::approve()`` is
what actually warns and commits it to the ledger.

**A week's leave over a public holiday costs four days, not five —
UNLESS this employer's own ``PublicHolidayObservance`` says otherwise**
(P6 chunk 3, task 4). ``is_working_day`` is FALSE for a rest day AND for a
public holiday inside the span — neither deducts, neither is paid, and
both still get their own ``leave_application_day`` row so the audit can
show why. Which dates count as a holiday for THIS employer is decided by
``_is_observed_holiday()`` — an explicit observance row wins over the
statutory calendar in either direction; see that function and
``PublicHolidayObservance``'s own docstring for the compliance note this
carries.

FLAGGED: a ``balance_source='parent'`` type (``ANNUAL_UNAUTHORISED``) is not
resolved to its PARENT's cycle here — ``ensure_cycles``/``balance_as_at``
only ever generate or read a cycle for ``balance_source='own'`` types, so an
application against a parent-drawing type always sees a zero balance and is
treated as fully overdrawn. Genuinely applying for unauthorised leave
through this flow needs that resolution built; nothing in this chunk's own
task list exercises it, and inventing the resolution without a cited shape
to build it against would be guessing. Recorded rather than silently wrong.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from attendance import scheduling
from core.managers import tenant_context_of
from employees.models import EmployeeLeaveEntitlement
from leave.cycles import accrual_method_for, ensure_cycles, unit_for_method
from leave.evidence import SICK_CERTIFICATE_THRESHOLD_PARAMETER
from leave.models import (
    LeaveApplication,
    LeaveApplicationDay,
    LeaveCycle,
    LeaveEvidenceType,
    LeaveType,
    PublicHolidayObservance,
)
from statutory import resolve

ZERO = Decimal("0")
_QUANTUM = Decimal("0.001")
AccrualMethod = EmployeeLeaveEntitlement.AccrualMethod


class ApplicationRefusedError(Exception):
    """The application may not be created this way. Nothing was written."""


def _next_reference(tenant) -> str:
    """``LV-{year}-{sequence}``, per tenant. Never reused.

    Counts this tenant's own applications this year and adds one. A genuine
    race between two submissions in the same instant is caught by
    ``uniq_leave_application_reference`` rather than prevented here — the
    same trade this codebase makes nowhere else needing a lock, because the
    volumes involved (one household or one cleaning firm's own leave
    requests) make the race vanishingly unlikely and the constraint means it
    can never silently double up.
    """
    year = timezone.localdate().year
    count = LeaveApplication.objects.filter(
        tenant=tenant, reference__startswith=f"LV-{year}-"
    ).count()
    return f"LV-{year}-{count + 1:05d}"


def _is_sick_leave_paid(
    leave_type: LeaveType,
    evidence_type: LeaveEvidenceType | None,
    *,
    working_units: Decimal,
    on_date: datetime.date,
) -> bool:
    """BCEA s23(1): independent evidence pays regardless of length; with
    none, the whole period is paid only within the statutory threshold.

    Raises if the threshold is not loaded — the same "resolve raises when
    missing" rule this codebase applies everywhere a statutory figure could
    otherwise be silently skipped rather than read.
    """
    if leave_type.code != LeaveType.Code.SICK:
        return True
    if evidence_type is not None and evidence_type.requires_attachment:
        return True

    threshold = resolve.parameter_value(SICK_CERTIFICATE_THRESHOLD_PARAMETER, on_date)
    return working_units <= threshold


def _is_observed_holiday(employer, a_date: datetime.date) -> bool:
    """Whether ``a_date`` is treated as a holiday for THIS employer — task
    4's observance override, checked before the statutory calendar.

    An explicit ``PublicHolidayObservance`` row wins outright, in EITHER
    direction: ``is_observed=False`` on a genuine statutory holiday means
    this employer's employees worked it as ordinary (see the model's own
    compliance note — this records an agreement, it does not make one), and
    an employer-specific row with no ``public_holiday`` at all can declare a
    day off the statutory calendar knows nothing about. No row falls back to
    the plain calendar exactly as chunk 2 read it.
    """
    observance = PublicHolidayObservance.objects.filter(
        employer=employer, observance_date=a_date
    ).first()
    if observance is not None:
        return observance.is_observed
    return resolve.is_public_holiday(a_date)


def _build_days(
    employee,
    leave_type: LeaveType,
    *,
    start_date: datetime.date,
    end_date: datetime.date,
    is_part_day: bool,
    unit: str,
) -> list[dict]:
    """The shape of every calendar date in the span — before pay or balance
    are known. One dict per date, in order.
    """
    days = []
    for a_date in scheduling.iter_dates(start_date, end_date):
        schedule = scheduling.current_schedule(employee, a_date)
        schedule_day = scheduling.schedule_day_for(schedule, a_date)
        is_public_holiday = _is_observed_holiday(employee.employer, a_date)
        is_working_day = bool(
            schedule_day is not None and schedule_day.is_working_day and not is_public_holiday
        )

        portion = Decimal("0.500") if is_part_day else Decimal("1.000")
        hours = None
        if is_working_day and unit == LeaveCycle.Unit.HOURS:
            scheduled_hours = schedule_day.ordinary_hours if schedule_day else ZERO
            hours = (scheduled_hours * portion).quantize(_QUANTUM, rounding=ROUND_HALF_UP)

        days.append(
            {
                "leave_date": a_date,
                "day_portion": portion,
                "hours": hours,
                "is_working_day": is_working_day,
                "is_public_holiday": is_public_holiday,
                "is_paid": True,
                "deducted_from_balance": is_working_day,
            }
        )
    return days


def submit_application(
    employee,
    *,
    leave_type: LeaveType,
    start_date: datetime.date,
    end_date: datetime.date,
    reason: str = "",
    leave_evidence_type: LeaveEvidenceType | None = None,
    is_part_day: bool = False,
    submitted_by=None,
) -> LeaveApplication:
    """Create and submit a leave application. Atomic. Never refuses for want
    of evidence, and never refuses for being overdrawn — see the module
    docstring.

    ``is_part_day`` is only meaningful for a single-day application — half
    days are the minimum increment for a salaried basis, and this chunk does
    not attempt a half-day-in-the-middle-of-a-longer-span shape sheet 02
    does not ask for either.
    """
    if end_date < start_date:
        raise ApplicationRefusedError(
            f"{end_date:%d %B %Y} is before {start_date:%d %B %Y} — an application "
            f"cannot end before it starts."
        )
    if is_part_day and start_date != end_date:
        raise ApplicationRefusedError(
            "A part-day application must cover exactly one date. Half days are the "
            "minimum increment for a salaried basis; a multi-day half-day span is "
            "not a shape this chunk supports."
        )

    with transaction.atomic(), tenant_context_of(employee):
        method, _entitlement = accrual_method_for(employee, leave_type, start_date)
        unit = unit_for_method(method)

        ensure_cycles(employee, leave_type, horizon=start_date)
        from leave.balances import balance_as_at

        cycle = balance_as_at(employee, leave_type, start_date)
        available = cycle.balance_quantity if cycle is not None else ZERO

        day_dicts = _build_days(
            employee,
            leave_type,
            start_date=start_date,
            end_date=end_date,
            is_part_day=is_part_day,
            unit=unit,
        )

        quantity_field = "day_portion" if unit == LeaveCycle.Unit.DAYS else "hours"
        requested = sum((d[quantity_field] for d in day_dicts if d["deducted_from_balance"]), ZERO)

        exceeds_balance = requested > available
        unpaid_units = max(requested - available, ZERO) if exceeds_balance else ZERO

        # The excess falls to unpaid, working BACKWARD from the last day in
        # the span — never refused, never silently paid from a balance that
        # is not there (task 2).
        remaining_unpaid = unpaid_units
        for day_dict in reversed(day_dicts):
            if not day_dict["deducted_from_balance"] or remaining_unpaid <= 0:
                continue
            day_dict["is_paid"] = False
            day_dict["deducted_from_balance"] = False
            remaining_unpaid -= day_dict[quantity_field]

        working_units_for_sick = sum(
            (d[quantity_field] for d in day_dicts if d["is_working_day"]), ZERO
        )
        sick_is_paid = _is_sick_leave_paid(
            leave_type,
            leave_evidence_type,
            working_units=working_units_for_sick,
            on_date=start_date,
        )
        if not sick_is_paid:
            for day_dict in day_dicts:
                if day_dict["is_working_day"]:
                    day_dict["is_paid"] = False
        elif not leave_type.is_paid:
            for day_dict in day_dicts:
                if day_dict["is_working_day"] and day_dict["deducted_from_balance"]:
                    day_dict["is_paid"] = False

        total_days = (
            sum((d["day_portion"] for d in day_dicts if d["is_working_day"]), ZERO)
            if unit == LeaveCycle.Unit.DAYS
            else ZERO
        )
        total_hours = (
            sum((d["hours"] or ZERO for d in day_dicts if d["is_working_day"]), ZERO)
            if unit == LeaveCycle.Unit.HOURS
            else None
        )

        application = LeaveApplication(
            tenant=employee.tenant,
            employee=employee,
            reference=_next_reference(employee.tenant),
            leave_type=leave_type,
            leave_evidence_type=leave_evidence_type,
            start_date=start_date,
            end_date=end_date,
            total_days=total_days,
            total_hours=total_hours,
            is_part_day=is_part_day,
            reason=reason,
            status=LeaveApplication.Status.SUBMITTED,
            submitted_by_user=submitted_by,
            submitted_at=timezone.now(),
            balance_at_submission=available,
            exceeds_balance=exceeds_balance,
            # FLAGGED: sheet 02 gives leave_application one unpaid_days column,
            # no unpaid_hours variant. An hourly-accrual employee's own overdraw
            # (unpaid_units in HOURS) has nowhere to be recorded at the
            # application level — exceeds_balance still flips, but the exact
            # hour figure is only visible per-day (is_paid/deducted_from_balance
            # on leave_application_day), not summarised here. Recorded rather
            # than silently reporting zero as if nothing were overdrawn.
            unpaid_days=unpaid_units if unit == LeaveCycle.Unit.DAYS else ZERO,
        )
        application.full_clean()
        application.save()

        for day_dict in day_dicts:
            LeaveApplicationDay.objects.create(
                tenant=employee.tenant, leave_application=application, **day_dict
            )

    return application
