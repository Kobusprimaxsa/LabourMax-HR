"""Tenant isolation — layer 3 of three.

This suite is GENERATED from the Django model registry. Every model inheriting
``TenantScopedModel`` is discovered and tested automatically, so a model added
next year is covered the day it is written rather than the day someone
remembers to write a test for it.

If this file fails, nothing merges. See CLAUDE.md, non-negotiable 1.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import connection

from core.managers import TenantScopedManager, get_current_tenant_id, tenant_context
from core.models import Tenant, TenantScopedModel


def tenant_scoped_models():
    """Every concrete model that inherits TenantScopedModel."""
    return [
        m for m in apps.get_models() if issubclass(m, TenantScopedModel) and not m._meta.abstract
    ]


def model_ids(models_):
    return [f"{m._meta.app_label}.{m.__name__}" for m in models_]


MODELS = tenant_scoped_models()


# --------------------------------------------------------------- fixtures


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(trading_name="Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(trading_name="Tenant B")


# --------------------------------------------------------------- structural


@pytest.mark.isolation
def test_at_least_one_tenant_scoped_model_exists():
    """Guards against the suite silently passing because discovery broke."""
    assert MODELS, (
        "No tenant-scoped models found. Either discovery is broken or no model "
        "inherits TenantScopedModel — both are bugs."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", MODELS, ids=model_ids(MODELS))
def test_model_has_tenant_column(model):
    assert any(f.name == "tenant" for f in model._meta.fields), (
        f"{model.__name__} inherits TenantScopedModel but has no tenant field."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", MODELS, ids=model_ids(MODELS))
def test_default_manager_is_scoped(model):
    assert isinstance(model._default_manager, TenantScopedManager), (
        f"{model.__name__}.objects is not a TenantScopedManager. Its default "
        f"queryset would return every tenant's rows."
    )


@pytest.mark.isolation
@pytest.mark.parametrize("model", MODELS, ids=model_ids(MODELS))
def test_row_level_security_is_enabled(db, model):
    """Layer 2: PostgreSQL must refuse cross-tenant rows on its own.

    A migration that creates a tenant-scoped table without calling
    ``enable_rls()`` fails here.
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
        f"Row-level security is not enabled on {table}. Add "
        f"RunSQL(enable_rls('{table}'), disable_rls('{table}')) to its migration."
    )
    assert forced, (
        f"Row-level security is not FORCED on {table}. Without FORCE the table "
        f"owner — which is what the application connects as — bypasses the policy."
    )


# --------------------------------------------------------------- behavioural


@pytest.mark.isolation
def test_no_tenant_in_context_returns_nothing(db, tenant_a):
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
def test_all_tenants_manager_is_the_only_escape_hatch(db, tenant_a, tenant_b, django_user_model):
    """``all_tenants`` exists for the console and maintenance jobs.

    It is deliberately named so it is obvious in review and easy to grep for.
    """
    from core.models import TenantMembership

    for i, tenant in enumerate((tenant_a, tenant_b)):
        user = django_user_model.objects.create_user(
            email=f"u{i}@example.com", password="x", mobile_number=f"+2782000010{i}"
        )
        with tenant_context(tenant.id):
            TenantMembership.objects.create(
                tenant=tenant, user=user, role=TenantMembership.Role.OWNER
            )

    with tenant_context(tenant_a.id):
        assert TenantMembership.objects.count() == 1
        assert TenantMembership.all_tenants.count() == 2


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
