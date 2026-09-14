"""Approval — gated by the exceptions chunk 1's calculator already computes.

Moves ``captured`` days to ``approved``. Locked days are already frozen by
``core/db/rls.py``'s trigger — this module does not special-case them, and
does not need to: attempting to approve one hits the same trigger every other
write does, inside the same transaction, so nothing is approved from that
batch either. That is the guard actually doing its job rather than a second,
Python-side copy of it (task 1a).

``attendance_day`` has no dedicated "approved by" column — sheet 02 does not
give it one — so who approved is recorded the same way every other change to
the row is: ``updated_by_user``, via ``AuditMixin``, read back from
``audit_log`` alongside everything else that happened to the row.
"""

from __future__ import annotations

from django.db import transaction

from attendance.grid import employee_exceptions
from attendance.models import AttendanceDay
from calculators.attendance import Severity
from core.managers import tenant_context_of


class ApprovalRefusedError(Exception):
    """A blocking exception stands over this span. Nothing was approved."""


def approve(days: list[AttendanceDay], by_user) -> list[AttendanceDay]:
    """Approve every day in ``days``. Refuses — approving nothing — while any
    BLOCKING exception stands over the span each day's employee is part of.
    A warning does not block.
    """
    by_employee: dict[int, list[AttendanceDay]] = {}
    for day in days:
        by_employee.setdefault(day.employee_id, []).append(day)

    blocking_messages = []
    for employee_days in by_employee.values():
        # Pinned from the day, not the employee: `.employee` is itself a
        # tenant-scoped lookup, and there is nothing to pin it WITH before
        # this point except the day already in hand.
        with tenant_context_of(employee_days[0]):
            employee = employee_days[0].employee
            exceptions = employee_exceptions(employee, employee_days)
        for exception in exceptions:
            if exception.severity == Severity.BLOCKING:
                blocking_messages.append(
                    f"{employee} on {exception.work_date}: {exception.message}"
                )

    if blocking_messages:
        raise ApprovalRefusedError(
            f"{len(blocking_messages)} blocking exception(s) stand over this span "
            f"and must be resolved before approval: " + "; ".join(blocking_messages)
        )

    updated = []
    with transaction.atomic():
        for day in days:
            with tenant_context_of(day):
                day.status = AttendanceDay.Status.APPROVED
                day.updated_by_user = by_user
                day.save(update_fields=["status", "updated_by_user", "updated_at"])
                updated.append(day)
    return updated
