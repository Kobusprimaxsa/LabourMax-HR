"""The fast path of timesheet_summary's two-layer staleness (D-153).

A signal, not a check inside ``attendance/capture.py`` or the bulk importer:
connecting to ``AttendanceDay``'s own save and delete means every writer is
covered by construction, including the one that does not exist yet (chunk
3's import batch) and any future one nobody remembers to update. See
``attendance/summary.py`` for the ground-truth half of the pair.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save

from attendance.models import AttendanceDay, TimesheetSummary
from core.managers import tenant_context


def _covering_summary(instance: AttendanceDay) -> TimesheetSummary | None:
    """The summary whose period covers this day's work_date, if one exists yet.

    Resolved through the employee's current pay group cache (D-18) — the same
    cache the employee list already trusts to know which pay group an
    employee is in.
    """
    from payroll.models import PayPeriod

    if instance.employee.current_pay_group_id is None:
        return None

    period = PayPeriod.objects.filter(
        pay_group_id=instance.employee.current_pay_group_id,
        period_start__lte=instance.work_date,
        period_end__gte=instance.work_date,
    ).first()
    if period is None:
        return None

    return TimesheetSummary.objects.filter(employee=instance.employee, pay_period=period).first()


def _mark_stale(sender, instance: AttendanceDay, **kwargs):
    with tenant_context(instance.tenant_id):
        summary = _covering_summary(instance)
        if summary is not None and not summary.is_stale:
            TimesheetSummary.objects.filter(pk=summary.pk).update(is_stale=True)


def connect_signals():
    """Wired from ``AttendanceConfig.ready()``."""
    post_save.connect(
        _mark_stale, sender=AttendanceDay, dispatch_uid="attendance.staleness.post_save"
    )
    post_delete.connect(
        _mark_stale, sender=AttendanceDay, dispatch_uid="attendance.staleness.post_delete"
    )
