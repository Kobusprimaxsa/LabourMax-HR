"""The fast path of leave_cycle's two-layer staleness (mirrors D-153).

A signal on ``LeaveTransaction``'s own save — never a check inside
``leave/ledger.py`` — so every writer is covered by construction, including
the accrual engine and any future one nobody remembers to update. There is
no ``post_delete`` half: the ledger is append-only, so a row is never
deleted, only corrected by a reversal, which is itself a save and fires this
same signal. See ``leave/balances.py`` for the ground-truth half of the pair.
"""

from __future__ import annotations

from django.db.models.signals import post_save

from core.managers import tenant_context
from leave.models import LeaveCycle, LeaveTransaction


def _mark_stale(sender, instance: LeaveTransaction, **kwargs):
    with tenant_context(instance.tenant_id):
        LeaveCycle.objects.filter(pk=instance.leave_cycle_id, is_stale=False).update(is_stale=True)


def connect_signals():
    """Wired from ``LeaveConfig.ready()``."""
    post_save.connect(
        _mark_stale, sender=LeaveTransaction, dispatch_uid="leave.staleness.post_save"
    )
