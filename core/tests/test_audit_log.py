"""Change history — what it records, and what it must never record.

The masking tests matter more than the rest. An audit table is designed to be
kept for years, so an ID number written into it in clear is a longer-lived
problem than the same number in the row it came from.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest

from core.audit import (
    MASKED,
    AuditedModel,
    audit_actor,
    audit_event,
    audit_suspended,
    audited_models,
    is_sensitive,
    to_jsonable,
)
from core.managers import platform_context, tenant_context
from core.models import AppUser, AuditLog, Tenant, TenantMembership


def audit_rows(table_name, record_pk=None):
    """Read the trail from platform context - it spans tenants by nature."""
    with platform_context():
        qs = AuditLog.all_tenants.filter(table_name=table_name)
        if record_pk is not None:
            qs = qs.filter(record_pk=record_pk)
        return list(qs.order_by("id"))


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Audit Co")


# ------------------------------------------------------------------ structural


@pytest.mark.audit
def test_the_audit_table_is_not_itself_audited():
    """Recursion guard, asserted rather than assumed."""
    assert not issubclass(AuditLog, AuditedModel)


@pytest.mark.audit
def test_high_volume_and_secret_bearing_tables_are_not_audited():
    from core.models import BackgroundJob, LoginAudit, OtpChallenge

    for model in (LoginAudit, OtpChallenge, BackgroundJob):
        assert not issubclass(model, AuditedModel), (
            f"{model.__name__} is audited. It records its own events, carries "
            f"secrets, or is machine chatter - all three are reasons not to."
        )


@pytest.mark.audit
def test_something_is_audited():
    assert audited_models(), "Discovery is broken - no audited models found."


@pytest.mark.audit
@pytest.mark.parametrize(
    "field_name",
    [
        "password",
        "mfa_secret",
        "code_hash",
        "token_hash",
        "id_number",
        "bank_account_number",
        "otp_code",
    ],
)
def test_sensitive_name_fragments_catch_undeclared_fields(field_name):
    """The safety net, for the field someone forgets to declare."""

    class Dummy:
        audit_sensitive_fields = ()

    assert is_sensitive(Dummy, field_name)


@pytest.mark.audit
def test_ordinary_fields_are_not_masked():
    class Dummy:
        audit_sensitive_fields = ()

    for name in ("trading_name", "status", "occurred_at", "size_bytes"):
        assert not is_sensitive(Dummy, name)


@pytest.mark.audit
def test_decimals_are_serialised_as_strings_never_floats():
    """Invariant 6 applies to the audit trail too.

    A money value rounded through a float in the history is a value you cannot
    reconcile against the payslip it came from.
    """
    result = to_jsonable(Decimal("1234.5678"))
    assert result == "1234.5678"
    assert isinstance(result, str)


# ----------------------------------------------------------------- behavioural


@pytest.mark.audit
def test_insert_records_every_field(db):
    tenant = Tenant.objects.create(trading_name="Inserted Co")
    rows = audit_rows("tenant", tenant.pk)
    assert len(rows) == 1
    row = rows[0]
    assert row.operation == AuditLog.Operation.INSERT
    assert row.changed_fields["trading_name"]["new"] == "Inserted Co"


@pytest.mark.audit
def test_update_records_only_what_changed(db, tenant):
    tenant.trading_name = "Renamed Co"
    tenant.save()

    rows = audit_rows("tenant", tenant.pk)
    update = [r for r in rows if r.operation == AuditLog.Operation.UPDATE]
    assert len(update) == 1
    changed = update[0].changed_fields
    assert changed["trading_name"] == {"old": "Audit Co", "new": "Renamed Co"}
    assert "legal_name" not in changed, "An unchanged field was recorded as changed."
    assert "updated_at" not in changed, "updated_at is noise and must be excluded."


@pytest.mark.audit
def test_saving_with_no_changes_writes_nothing(db, tenant):
    before = len(audit_rows("tenant", tenant.pk))
    tenant.save()
    assert len(audit_rows("tenant", tenant.pk)) == before


@pytest.mark.audit
def test_delete_records_the_final_state(db):
    tenant = Tenant.objects.create(trading_name="Doomed Co")
    pk = tenant.pk
    tenant.delete()

    rows = audit_rows("tenant", pk)
    deletes = [r for r in rows if r.operation == AuditLog.Operation.DELETE]
    assert len(deletes) == 1
    assert deletes[0].changed_fields["trading_name"]["old"] == "Doomed Co"


@pytest.mark.audit
def test_password_is_recorded_as_changed_but_never_stored(db):
    user = AppUser.objects.create_user(
        email="audit@example.com", password="first-password", mobile_number="+27820000501"
    )
    user.set_password("second-password")
    user.save()

    rows = audit_rows("app_user", user.pk)
    serialised = str([r.changed_fields for r in rows])

    assert "first-password" not in serialised
    assert "second-password" not in serialised
    # The hash must not leak either - it is offline-attackable.
    assert "pbkdf2" not in serialised and "argon2" not in serialised

    updates = [r for r in rows if r.operation == AuditLog.Operation.UPDATE]
    assert updates, "A password change must still be recorded as having happened."
    assert updates[-1].changed_fields["password"] == {"changed": True, "masked": True}


@pytest.mark.audit
def test_insert_masks_sensitive_fields_too(db):
    user = AppUser.objects.create_user(
        email="masked@example.com", password="hunter2", mobile_number="+27820000502"
    )
    rows = audit_rows("app_user", user.pk)
    assert rows[0].changed_fields["password"]["new"] == MASKED
    assert "hunter2" not in str(rows[0].changed_fields)


@pytest.mark.audit
def test_actor_and_business_event_are_recorded(db, tenant):
    actor = AppUser.objects.create_user(
        email="actor@example.com", password="x", mobile_number="+27820000503"
    )

    with audit_actor(user_id=actor.pk, kind="user", ip_address="102.65.1.1"):
        with audit_event("tenant_renamed"):
            tenant.trading_name = "Under Actor"
            tenant.save()

    rows = [r for r in audit_rows("tenant", tenant.pk) if r.operation == "update"]
    assert rows[-1].actor_user_id == actor.pk
    assert rows[-1].actor_kind == "user"
    assert rows[-1].business_event == "tenant_renamed"
    assert rows[-1].ip_address == "102.65.1.1"


@pytest.mark.audit
def test_changes_without_an_actor_are_attributed_to_system(db, tenant):
    tenant.trading_name = "No Actor"
    tenant.save()
    rows = [r for r in audit_rows("tenant", tenant.pk) if r.operation == "update"]
    assert rows[-1].actor_kind == "system"
    assert rows[-1].actor_user_id is None


@pytest.mark.audit
def test_audit_row_carries_the_tenant_of_the_changed_row(db, tenant):
    user = AppUser.objects.create_user(
        email="member@example.com", password="x", mobile_number="+27820000504"
    )
    with tenant_context(tenant.pk):
        membership = TenantMembership.objects.create(
            tenant=tenant, user=user, role=TenantMembership.Role.OWNER
        )

    rows = audit_rows("tenant_membership", membership.pk)
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant.pk, (
        "An audit row for a tenant's change must belong to that tenant, or the "
        "tenant cannot read its own history."
    )


@pytest.mark.audit
def test_a_tenant_can_read_its_own_history_and_no_one_elses(db, tenant):
    other = Tenant.objects.create(trading_name="Other Co")
    users = [
        AppUser.objects.create_user(
            email=f"hist{i}@example.com", password="x", mobile_number=f"+2782000060{i}"
        )
        for i in range(2)
    ]
    for t, user in zip((tenant, other), users, strict=True):
        with tenant_context(t.pk):
            TenantMembership.objects.create(tenant=t, user=user, role=TenantMembership.Role.OWNER)

    with tenant_context(tenant.pk):
        visible = AuditLog.objects.filter(table_name="tenant_membership")
        assert visible.count() == 1
        assert all(r.tenant_id == tenant.pk for r in visible)


@pytest.mark.audit
def test_audit_suspended_writes_nothing(db):
    with audit_suspended():
        tenant = Tenant.objects.create(trading_name="Bulk Loaded Co")
        tenant.trading_name = "Bulk Renamed Co"
        tenant.save()

    assert audit_rows("tenant", tenant.pk) == []


@pytest.mark.audit
def test_the_trail_is_append_only_in_practice(db, tenant):
    """Belt and braces: the change history cannot be edited after the fact."""
    from django.db import DatabaseError, transaction

    tenant.trading_name = "Will Try To Tamper"
    tenant.save()
    row = [r for r in audit_rows("tenant", tenant.pk) if r.operation == "update"][-1]

    with platform_context(), pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            AuditLog.all_tenants.filter(pk=row.pk).update(business_event="rewritten")


@pytest.mark.audit
@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        (datetime.datetime(2026, 3, 1, 8, 30, tzinfo=datetime.UTC), str),
        (datetime.date(2026, 3, 1), str),
        (uuid.uuid4(), str),
        (Decimal("0.01"), str),
        (42, int),
        (True, bool),
        (None, type(None)),
        ("plain", str),
    ],
)
def test_every_field_type_survives_serialisation(value, expected_type):
    """JSONB cannot take a datetime or a Decimal.

    A TypeError here would not corrupt the audit trail - it would block the
    business save that triggered it, turning a record-keeping bug into an
    outage. Hence the exhaustive check rather than trusting the happy path.
    """
    assert isinstance(to_jsonable(value), expected_type)
