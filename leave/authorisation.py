"""Approving, declining and cancelling a leave application — P6 chunk 2, task 3.

**Approval writes the ``taken`` transaction(s) and the attendance days.**
Both happen here, in one transaction, because an approved application with
no ledger entry or no attendance row is a leave day nobody can see anywhere
else in the system.

**Self-approval is BLOCKED and escalates one level to the owner** (task 3,
a settled decision). Only where the deciding user IS the owner AND no other
approver (owner or admin) exists for this tenant may ``self_approved`` be
TRUE — and then only with a reason, which is never hidden: it is a column
on the row, read by the leave register like any other.

**Cancelling reverses the ledger — it never edits or deletes it**
(invariant 4, chunk 1's own reversal rule). A cancelled application after
the leave was already taken still reverses: the employer may have to
explain a late cancellation, and that is exactly what the trail is for.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from attendance.capture import LockedDayError, capture
from attendance.models import AttendanceDay
from core.managers import tenant_context_of
from core.models import TenantMembership
from leave.cycles import accrual_method_for, current_cycle, ensure_cycles, unit_for_method
from leave.ledger import post_transaction, reverse_transaction
from leave.models import LeaveApplication, LeaveCycle, LeaveTransaction

ZERO = Decimal("0")


class AuthorisationRefusedError(Exception):
    """The application may not be decided this way. Nothing was written."""


def _decider_membership(decided_by, tenant) -> TenantMembership | None:
    return TenantMembership.objects.filter(
        user=decided_by, tenant=tenant, is_active=True, revoked_at__isnull=True
    ).first()


def _is_self_approval(membership: TenantMembership | None, employee) -> bool:
    return membership is not None and membership.employee_id_ref == employee.pk


def _another_approver_exists(tenant, employee, exclude_membership: TenantMembership) -> bool:
    return (
        TenantMembership.objects.filter(
            tenant=tenant,
            is_active=True,
            revoked_at__isnull=True,
            role__in=[TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN],
        )
        .exclude(pk=exclude_membership.pk)
        .exclude(employee_id_ref=employee.pk)
        .exists()
    )


def _check_self_approval(application: LeaveApplication, decided_by) -> tuple[bool, str]:
    """Returns ``(self_approved, reason_required_message_or_empty)``.

    Raises rather than returning when the attempt must be refused outright.
    """
    membership = _decider_membership(decided_by, application.tenant)
    if not _is_self_approval(membership, application.employee):
        return False, ""

    if membership.role != TenantMembership.Role.OWNER:
        raise AuthorisationRefusedError(
            f"{decided_by} may not approve their own leave application. "
            f"Self-approval is blocked and escalates to the owner."
        )
    if _another_approver_exists(application.tenant, application.employee, membership):
        raise AuthorisationRefusedError(
            "Another approver is available for this tenant. The owner may only "
            "self-approve when no other approver exists."
        )
    return True, ""


def _conflicting_worked_day(employee, work_date) -> AttendanceDay | None:
    existing = AttendanceDay.objects.filter(employee=employee, work_date=work_date).first()
    if existing is None:
        return None
    worked_hours = (
        existing.ordinary_hours
        + existing.overtime_hours
        + existing.sunday_hours
        + existing.public_holiday_hours
    )
    if worked_hours > 0:
        return existing
    return None


def approve(
    application: LeaveApplication,
    *,
    decided_by,
    decision_comment: str = "",
    self_approval_reason: str = "",
) -> LeaveApplication:
    """Approve a submitted application. Atomic — writes the attendance days
    and the ledger transaction together, or writes neither.

    Refuses, naming the date, if a day in the span is already captured as
    worked — nobody is both at work and on leave. Refuses, naming the run,
    if a day in the span is locked by a finalised payroll run —
    ``attendance/capture.py``'s own guard, not duplicated here.
    """
    if application.status != LeaveApplication.Status.SUBMITTED:
        raise AuthorisationRefusedError(
            f"{application.reference} is {application.status}, not submitted. "
            f"Only a submitted application may be approved."
        )

    with transaction.atomic(), tenant_context_of(application):
        self_approved, _ = _check_self_approval(application, decided_by)
        if self_approved and not self_approval_reason:
            raise AuthorisationRefusedError(
                "Self-approval by the sole owner requires a reason — mandatory, "
                "and visible in the leave register, never hidden."
            )

        days = list(application.days.all())

        for day in days:
            if not day.is_working_day:
                continue
            conflict = _conflicting_worked_day(application.employee, day.leave_date)
            if conflict is not None:
                raise AuthorisationRefusedError(
                    f"{day.leave_date:%d %B %Y} is already captured as worked for "
                    f"{application.employee}. Nobody is both at work and on leave — "
                    f"fix the attendance day or the application before approving."
                )

        employee = application.employee
        leave_type = application.leave_type
        method, _entitlement = accrual_method_for(employee, leave_type, application.start_date)
        unit = unit_for_method(method)

        ensure_cycles(employee, leave_type, horizon=application.start_date)
        cycle = current_cycle(employee, leave_type, application.start_date)

        deducted_quantity = ZERO
        for day in days:
            if not day.is_working_day:
                continue
            try:
                capture(
                    employee=employee,
                    work_date=day.leave_date,
                    day_type=AttendanceDay.DayType.LEAVE,
                    leave_application=application,
                    leave_day_portion=day.day_portion,
                    source=AttendanceDay.Source.GENERATED,
                )
            except LockedDayError as error:
                raise AuthorisationRefusedError(str(error)) from error

            if day.deducted_from_balance:
                quantity = day.day_portion if unit == LeaveCycle.Unit.DAYS else (day.hours or ZERO)
                deducted_quantity += quantity

        if deducted_quantity > 0:
            if cycle is None:
                raise AuthorisationRefusedError(
                    f"No {leave_type.code} cycle covers {application.start_date:%d %B %Y} "
                    f"for {employee}. Nothing was written."
                )
            post_transaction(
                employee=employee,
                leave_cycle=cycle,
                leave_type=leave_type,
                transaction_type=LeaveTransaction.TransactionType.TAKEN,
                quantity=-deducted_quantity,
                unit=unit,
                transaction_date=application.start_date,
                calculation_basis="manual",
                leave_application=application,
                created_by=decided_by,
            )

        application.status = LeaveApplication.Status.APPROVED
        application.decided_by_user = decided_by
        application.decided_at = timezone.now()
        application.decision_comment = decision_comment
        application.self_approved = self_approved
        application.self_approval_reason = self_approval_reason if self_approved else ""
        application.full_clean()
        application.save()

    return application


def decline(
    application: LeaveApplication, *, decided_by, decision_comment: str = ""
) -> LeaveApplication:
    """Decline a submitted application. Writes nothing to the ledger or to
    attendance — a declined application never happened, unlike a cancelled
    one which reverses what did.
    """
    if application.status != LeaveApplication.Status.SUBMITTED:
        raise AuthorisationRefusedError(
            f"{application.reference} is {application.status}, not submitted. "
            f"Only a submitted application may be declined."
        )

    with transaction.atomic(), tenant_context_of(application):
        application.status = LeaveApplication.Status.DECLINED
        application.decided_by_user = decided_by
        application.decided_at = timezone.now()
        application.decision_comment = decision_comment
        application.full_clean()
        application.save()

    return application


def cancel(
    application: LeaveApplication, *, cancelled_reason: str, cancelled_by=None
) -> LeaveApplication:
    """Cancel an approved application — REVERSES its ledger transactions
    (invariant 4) and removes the attendance days it wrote. Never edits or
    deletes a ledger row.

    Permitted even after the leave was taken: the employer may have to
    explain a late cancellation, and reversing rather than deleting is what
    leaves that explanation possible.
    """
    if application.status != LeaveApplication.Status.APPROVED:
        raise AuthorisationRefusedError(
            f"{application.reference} is {application.status}, not approved. "
            f"Only an approved application may be cancelled."
        )
    if not cancelled_reason:
        raise AuthorisationRefusedError("A cancellation needs a reason, recorded on the record.")

    with transaction.atomic(), tenant_context_of(application):
        for txn in LeaveTransaction.objects.filter(
            leave_application=application, transaction_type=LeaveTransaction.TransactionType.TAKEN
        ):
            reverse_transaction(
                txn,
                reason=f"{application.reference} cancelled: {cancelled_reason}",
                created_by=cancelled_by,
            )

        for day in AttendanceDay.objects.filter(leave_application=application):
            if day.status == AttendanceDay.Status.LOCKED:
                raise AuthorisationRefusedError(
                    f"{day.work_date:%d %B %Y} is locked by payroll run "
                    f"{day.locked_by_payroll_run_id_ref} and cannot be removed. A "
                    f"correction reverses and replaces that run instead."
                )
            day.delete()

        application.status = LeaveApplication.Status.CANCELLED
        application.cancelled_reason = cancelled_reason
        application.full_clean()
        application.save()

    return application
