"""Tenant isolation — layer 3 of three.

This suite is GENERATED from the Django model registry. Every model inheriting
``TenantScopedModel``, ``TenantOptionalModel`` or ``TenantSharedModel`` is
discovered and tested automatically, so a model added next year is covered the
day it is written rather than the day someone remembers to write a test for it.

The three bases are tested differently on purpose:

``TenantScopedModel``  tenant_id NOT NULL, strict policy, no platform override.
``TenantOptionalModel``  tenant_id nullable, and a tenant must still never see
                         the NULL-tenant rows.
``TenantSharedModel``  tenant_id nullable, and a tenant SHOULD see the NULL-tenant
                       rows — they are the shared catalogue — but must still never
                       see another tenant's, and must never write a shared one.

The one guarantee that holds across all three, and the only one that ends this
product if it breaks: no tenant ever sees another tenant's rows.

If this file fails, nothing merges. See CLAUDE.md, non-negotiable 1.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import connection

from core.managers import (
    TenantOptionalManager,
    TenantScopedManager,
    TenantSharedManager,
    get_current_tenant_id,
    platform_context,
    tenant_context,
)
from core.models import Tenant, TenantOptionalModel, TenantScopedModel, TenantSharedModel


def _concrete(base):
    return [m for m in apps.get_models() if issubclass(m, base) and not m._meta.abstract]


def model_ids(models_):
    return [f"{m._meta.app_label}.{m.__name__}" for m in models_]


SCOPED = _concrete(TenantScopedModel)
OPTIONAL = _concrete(TenantOptionalModel)
SHARED = _concrete(TenantSharedModel)
NULLABLE_TENANT = OPTIONAL + SHARED
ALL_TENANT_TABLES = SCOPED + OPTIONAL + SHARED


# --------------------------------------------------------------- fixtures


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(trading_name="Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(trading_name="Tenant B")


# --------------------------------------------------------------- structural


@pytest.mark.isolation
def test_discovery_found_both_kinds_of_model():
    """Guards against the suite silently passing because discovery broke."""
    assert SCOPED, "No TenantScopedModel subclasses found - discovery is broken."
    assert OPTIONAL, "No TenantOptionalModel subclasses found - discovery is broken."
    assert SHARED, "No TenantSharedModel subclasses found - discovery is broken."


@pytest.mark.isolation
def test_no_model_carries_a_tenant_column_without_a_base():
    """The gap this suite used to have.

    A model can grow a ``tenant`` field without inheriting either base, and it
    then gets no scoped manager, no RLS policy, and no coverage here — while the
    suite still reports green. That is how ``file_object`` came to hold CVs and
    ID documents with no isolation at all. Close the door rather than trusting
    everyone to remember.
    """
    offenders = []
    for model in apps.get_models():
        if model._meta.app_label in {"auth", "contenttypes", "sessions", "admin"}:
            continue
        if issubclass(model, (TenantScopedModel, TenantOptionalModel, TenantSharedModel)):
            continue
        if any(f.name == "tenant" for f in model._meta.fields):
            offenders.append(f"{model._meta.app_label}.{model.__name__}")

    assert not offenders, (
        "These models have a tenant column but inherit none of TenantScopedModel, "
        "TenantOptionalModel or TenantSharedModel, so they have no scoped manager, "
        f"no row-level security policy and no isolation test: {offenders}"
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", ALL_TENANT_TABLES, ids=model_ids(ALL_TENANT_TABLES))
def test_model_has_tenant_column(model):
    assert any(f.name == "tenant" for f in model._meta.fields), (
        f"{model.__name__} inherits a tenant base but has no tenant field."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", SCOPED, ids=model_ids(SCOPED))
def test_scoped_tenant_column_is_not_nullable(model):
    field = model._meta.get_field("tenant")
    assert not field.null, (
        f"{model.__name__}.tenant is nullable but the model inherits "
        f"TenantScopedModel, whose RLS policy can never match a NULL. Either make "
        f"it NOT NULL or move the model to TenantOptionalModel deliberately."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", NULLABLE_TENANT, ids=model_ids(NULLABLE_TENANT))
def test_nullable_tenant_column_is_actually_nullable(model):
    field = model._meta.get_field("tenant")
    assert field.null, (
        f"{model.__name__}.tenant is NOT NULL, so it should inherit "
        f"TenantScopedModel and get the strict policy."
    )


@pytest.mark.isolation
def test_no_model_inherits_two_tenant_bases():
    """The two nullable bases mean OPPOSITE things by a NULL tenant_id.

    On ``TenantOptionalModel`` a NULL row is the platform's and no tenant may see
    it. On ``TenantSharedModel`` a NULL row is everybody's. A model inheriting
    both would get whichever manager the MRO happened to pick and a policy chosen
    by whichever helper its migration called - and the two disagreeing is exactly
    the shape of bug that leaks rows quietly.
    """
    bases = (TenantScopedModel, TenantOptionalModel, TenantSharedModel)
    offenders = [
        f"{m._meta.app_label}.{m.__name__}"
        for m in apps.get_models()
        if sum(issubclass(m, base) for base in bases) > 1
    ]
    assert not offenders, f"These models inherit more than one tenant base: {offenders}"


@pytest.mark.isolation
@pytest.mark.parametrize("model", SHARED, ids=model_ids(SHARED))
def test_shared_default_manager(model):
    assert isinstance(model._default_manager, TenantSharedManager), (
        f"{model.__name__}.objects is {type(model._default_manager).__name__}, not a "
        f"TenantSharedManager."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", SHARED, ids=model_ids(SHARED))
def test_shared_tables_hold_no_employer_or_employee_data(model):
    """The standing test of whether a table belongs on the shared base.

    A shared row is visible to every tenant on the platform, so the question is
    whether a row with no tenant would be safe on a competitor's screen. A
    component definition is. Anything naming a person, or carrying an amount
    somebody was actually paid, is not - and would be a leak that no policy here
    would report, because the policy is doing exactly what it was asked to.
    """
    forbidden = {"employee", "employer", "payslip", "pay_period", "amount", "id_number"}
    names = {f.name for f in model._meta.fields}
    assert not (names & forbidden), (
        f"{model.__name__} inherits TenantSharedModel but carries {sorted(names & forbidden)}. "
        f"A shared row is readable by every tenant. Use TenantScopedModel."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", SCOPED, ids=model_ids(SCOPED))
def test_scoped_default_manager(model):
    assert isinstance(model._default_manager, TenantScopedManager), (
        f"{model.__name__}.objects is {type(model._default_manager).__name__}, not a "
        f"TenantScopedManager. A plain Manager declared on the concrete model "
        f"shadows the inherited one and its default queryset returns every "
        f"tenant's rows."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", OPTIONAL, ids=model_ids(OPTIONAL))
def test_optional_default_manager(model):
    assert isinstance(model._default_manager, TenantOptionalManager), (
        f"{model.__name__}.objects is {type(model._default_manager).__name__}, not a "
        f"TenantOptionalManager."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", ALL_TENANT_TABLES, ids=model_ids(ALL_TENANT_TABLES))
def test_row_level_security_is_enabled_and_forced(db, model):
    """Layer 2: PostgreSQL must refuse cross-tenant rows on its own.

    A migration that creates a tenant table without calling ``enable_rls()`` or
    ``enable_rls_optional()`` fails here.
    """
    table = model._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            [table],
        )
        row = cursor.fetchone()

    assert row is not None, f"Table {table} does not exist."
    enabled, forced = row
    assert enabled, (
        f"Row-level security is not enabled on {table}. Add the RunSQL operation to its migration."
    )
    assert forced, (
        f"Row-level security is not FORCED on {table}. Without FORCE the table "
        f"owner - which is what the application connects as - bypasses the policy, "
        f"so this suite would pass while the protection did nothing."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", ALL_TENANT_TABLES, ids=model_ids(ALL_TENANT_TABLES))
def test_policy_exists(db, model):
    table = model._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute("SELECT policyname FROM pg_policies WHERE tablename = %s", [table])
        policies = [r[0] for r in cursor.fetchall()]
    assert "tenant_isolation" in policies, (
        f"{table} has RLS enabled but no tenant_isolation policy. RLS with no "
        f"policy denies everything, which will look like an empty database."
    )


@pytest.mark.isolation
def test_application_role_is_not_a_superuser(db):
    """A superuser bypasses RLS entirely, making every test above meaningless."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cursor.fetchone()
    assert row is not None
    assert not row[0], (
        "The test database connection is a PostgreSQL superuser. Superusers "
        "bypass row-level security, so every RLS assertion in this suite would "
        "pass without the protection existing. Point the tests at an ordinary role."
    )


# --------------------------------------------------------------- behavioural


@pytest.mark.isolation
def test_no_tenant_in_context_returns_nothing_for_scoped(db, tenant_a):
    """An unscoped read must return nothing, not everything.

    Empty results are a visible bug. Leaked rows are an invisible one.
    """
    from core.models import TenantMembership

    with tenant_context(None):
        assert get_current_tenant_id() is None
        assert TenantMembership.objects.count() == 0


@pytest.mark.isolation
def test_tenant_a_cannot_read_tenant_b(db, tenant_a, tenant_b, django_user_model):
    from core.models import TenantMembership

    user_b = django_user_model.objects.create_user(
        email="b@example.com", password="x", mobile_number="+27820000002"
    )
    with tenant_context(tenant_b.id):
        TenantMembership.objects.create(
            tenant=tenant_b, user=user_b, role=TenantMembership.Role.OWNER
        )

    with tenant_context(tenant_a.id):
        assert TenantMembership.objects.count() == 0
        assert not TenantMembership.objects.filter(user=user_b).exists()

    with tenant_context(tenant_b.id):
        assert TenantMembership.objects.count() == 1


@pytest.mark.isolation
def test_tenant_a_cannot_update_or_delete_tenant_b(db, tenant_a, tenant_b, django_user_model):
    from core.models import TenantMembership

    user_b = django_user_model.objects.create_user(
        email="b2@example.com", password="x", mobile_number="+27820000003"
    )
    with tenant_context(tenant_b.id):
        membership = TenantMembership.objects.create(
            tenant=tenant_b, user=user_b, role=TenantMembership.Role.OWNER
        )

    with tenant_context(tenant_a.id):
        assert TenantMembership.objects.filter(pk=membership.pk).update(is_active=False) == 0
        assert TenantMembership.objects.filter(pk=membership.pk).delete()[0] == 0

    with tenant_context(tenant_b.id):
        membership.refresh_from_db()
        assert membership.is_active is True


@pytest.mark.isolation
def test_row_level_security_blocks_the_orm_escape_hatch(db, tenant_a, tenant_b, django_user_model):
    """The test that proves layer 2 is real rather than decorative.

    ``all_tenants`` bypasses the manager filter completely. It must STILL return
    only the pinned tenant's rows, because PostgreSQL is enforcing the policy
    underneath. If this test fails while the others pass, layer 1 is carrying the
    whole system on its own.
    """
    from core.models import TenantMembership

    for i, tenant in enumerate((tenant_a, tenant_b)):
        user = django_user_model.objects.create_user(
            email=f"esc{i}@example.com", password="x", mobile_number=f"+2782000020{i}"
        )
        with tenant_context(tenant.id):
            TenantMembership.objects.create(
                tenant=tenant, user=user, role=TenantMembership.Role.OWNER
            )

    with tenant_context(tenant_a.id):
        assert TenantMembership.objects.count() == 1
        assert TenantMembership.all_tenants.count() == 1, (
            "all_tenants returned another tenant's rows. Row-level security is not doing its job."
        )


@pytest.mark.isolation
def test_platform_context_does_not_open_employer_data(db, tenant_a, tenant_b, django_user_model):
    """Decision D-52: strictly scoped tables have NO platform override.

    ``platform_context()`` is not a master key to employee records. The superuser
    console reads employer data by entering each tenant's context in turn, which
    is auditable per tenant; reading around the policy would not be.

    This asserts a deliberate limitation, so if it ever fails because someone
    added a platform clause to ``enable_rls``, that is the conversation to have
    rather than the test to delete.
    """
    from core.models import TenantMembership

    user = django_user_model.objects.create_user(
        email="pc@example.com", password="x", mobile_number="+27820000301"
    )
    with tenant_context(tenant_a.id):
        TenantMembership.objects.create(
            tenant=tenant_a, user=user, role=TenantMembership.Role.OWNER
        )

    with platform_context():
        assert TenantMembership.all_tenants.count() == 0

    with tenant_context(tenant_a.id):
        assert TenantMembership.objects.count() == 1


@pytest.mark.isolation
def test_tenant_cannot_see_platform_rows_on_optional_tables(db, tenant_a):
    """The rule that makes a nullable tenant_id safe.

    A platform-level audit row has no tenant. No TENANT session may read it —
    that is the guarantee that matters. A session with no tenant pinned can,
    because Django's ``RETURNING id`` means a row the USING clause rejects cannot
    be inserted at all; see ``enable_rls_optional``.
    """
    from core.models import AuditLog

    probe = {"business_event": "isolation-probe-platform"}

    with tenant_context(None):
        AuditLog.objects.create(
            table_name="tenant",
            record_pk="0",
            operation=AuditLog.Operation.INSERT,
            **probe,
        )
        # Same context that wrote it can read it back.
        assert AuditLog.objects.filter(**probe).count() == 1

    with tenant_context(tenant_a.id):
        assert AuditLog.objects.filter(**probe).count() == 0
        assert AuditLog.all_tenants.filter(**probe).count() == 0

    with platform_context():
        assert AuditLog.all_tenants.filter(**probe).count() == 1


@pytest.mark.isolation
def test_optional_table_tenant_rows_are_still_isolated(db, tenant_a, tenant_b):
    from core.models import AuditLog

    probe = {"business_event": "isolation-probe-tenant"}

    with tenant_context(tenant_b.id):
        AuditLog.objects.create(
            tenant=tenant_b,
            table_name="tenant_membership",
            record_pk="1",
            operation=AuditLog.Operation.UPDATE,
            **probe,
        )

    with tenant_context(tenant_a.id):
        assert AuditLog.objects.filter(**probe).count() == 0

    with tenant_context(tenant_b.id):
        assert AuditLog.objects.filter(**probe).count() == 1


# --------------------------------------------------------------- business rules


@pytest.mark.isolation
def test_membership_can_be_revoked_then_regranted(db, tenant_a, django_user_model):
    """Decision D-06.

    The unique constraint applies only to live memberships, so a role can be
    filled again after someone leaves — by the same person or a different one.
    """
    from django.utils import timezone

    from core.models import TenantMembership

    user = django_user_model.objects.create_user(
        email="returner@example.com", password="x", mobile_number="+27820000004"
    )
    with tenant_context(tenant_a.id):
        first = TenantMembership.objects.create(
            tenant=tenant_a, user=user, role=TenantMembership.Role.ADMIN
        )
        first.revoked_at = timezone.now()
        first.save()

        second = TenantMembership.objects.create(
            tenant=tenant_a, user=user, role=TenantMembership.Role.ADMIN
        )
        assert second.pk != first.pk
        assert TenantMembership.all_tenants.filter(tenant=tenant_a, user=user).count() == 2


@pytest.mark.isolation
def test_a_tenant_sees_shared_rows_but_not_another_tenants(db, tenant_a, tenant_b):
    """The shared base's whole reason to exist, and its whole risk.

    ``TenantOptionalModel`` and ``TenantSharedModel`` both allow a NULL tenant_id
    and mean opposite things by it. This is the assertion that tells them apart, and
    it sits next to ``test_tenant_cannot_see_platform_rows_on_optional_tables`` so
    that the two opposite expectations are read together rather than one at a time.

    What does NOT change between them: tenant A never sees tenant B's rows.
    """
    from employers.models import PayrollComponent

    with platform_context():
        PayrollComponent.objects.create(
            tenant=None,
            code="ISOLATION_PROBE_SHARED",
            name="Shared probe",
            component_type=PayrollComponent.ComponentType.INFORMATIONAL,
            is_system=True,
        )
    with tenant_context(tenant_b.id):
        PayrollComponent.objects.create(
            tenant=tenant_b,
            code="ISOLATION_PROBE_B",
            name="B's probe",
            component_type=PayrollComponent.ComponentType.INFORMATIONAL,
        )

    with tenant_context(tenant_a.id):
        codes = set(PayrollComponent.objects.values_list("code", flat=True))
        assert "ISOLATION_PROBE_SHARED" in codes, (
            "A shared row is readable by every tenant - that is what the base is for."
        )
        assert "ISOLATION_PROBE_B" not in codes
        assert not PayrollComponent.all_tenants.filter(code="ISOLATION_PROBE_B").exists(), (
            "all_tenants bypasses the manager, not the policy. Layer 2 is not working."
        )


@pytest.mark.isolation
def test_a_tenant_cannot_write_a_shared_row(db, tenant_a):
    """Read and write differ on the shared base, and only the policy can say so.

    A manager filters reads. It cannot stop a write, so if the WITH CHECK clause
    ever loses its second half, one tenant can add a row to the catalogue every
    other tenant reads.
    """
    from django.db import DatabaseError, transaction

    from employers.models import PayrollComponent

    with (
        tenant_context(tenant_a.id),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        PayrollComponent.objects.create(
            tenant=None,
            code="ISOLATION_PROBE_SNEAK",
            name="Into everyone's catalogue",
            component_type=PayrollComponent.ComponentType.INFORMATIONAL,
            is_system=True,
        )
