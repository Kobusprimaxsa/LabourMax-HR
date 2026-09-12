"""Invariant 4: the audit trail is never rewritten.

Enforced by a database trigger rather than a privilege revoke, because the
application connects as the table owner and an owner is not bound by a revoke
against PUBLIC. This suite exists to prove the trigger actually bites — the
previous implementation looked correct and did nothing at all.
"""

from __future__ import annotations

import pytest
from django.db import DatabaseError, connection, transaction

from core.managers import tenant_context
from core.models import AuditLog, Tenant


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Append Only Co")


def _make_row(tenant):
    with tenant_context(tenant.id):
        return AuditLog.objects.create(
            tenant=tenant,
            table_name="tenant_membership",
            record_pk="1",
            operation=AuditLog.Operation.INSERT,
        )


@pytest.mark.isolation
def test_audit_log_update_is_refused(db, tenant):
    row = _make_row(tenant)
    with tenant_context(tenant.id), pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            AuditLog.objects.filter(pk=row.pk).update(business_event="tampered")


@pytest.mark.isolation
def test_audit_log_delete_is_refused(db, tenant):
    row = _make_row(tenant)
    with tenant_context(tenant.id), pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            AuditLog.objects.filter(pk=row.pk).delete()


@pytest.mark.isolation
def test_retention_purge_may_delete_deliberately(db, tenant):
    """The one exception: the POPIA retention purge (P11).

    It must be able to delete, so the trigger honours an explicit session flag.
    If this test ever fails, statutory retention cannot be implemented.
    """
    row = _make_row(tenant)
    with tenant_context(tenant.id):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('labourmax.allow_ledger_maintenance', 'on', true)")
        deleted, _ = AuditLog.objects.filter(pk=row.pk).delete()
        assert deleted == 1
