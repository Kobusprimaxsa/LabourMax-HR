"""``document_category`` — the third shared table, and the D-134 case again.

The column D-134 fixed for ``leave_type`` ships WITH this table from its first
migration, so these tests exist to prove the bug has no window to occur here:
a tenant must be able to rename and delete its own category the moment the
table exists, not after a retrofit.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

from core.managers import tenant_context
from core.models import Tenant
from documents.categories import seed_system_categories
from documents.models import DocumentCategory
from statutory.models import Sector

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Household One")


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(trading_name="Household Two")


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )


@pytest.fixture
def seeded(db, cleaning):
    """The shared catalogue, seeded once for tests that read it."""
    return seed_system_categories()


def test_a_tenant_reads_the_seeded_catalogue_and_its_own_categories(tenant, other_tenant, seeded):
    with tenant_context(tenant.pk):
        DocumentCategory.objects.create(
            tenant=tenant, code="HOUSE_RULES", name="Signed house rules"
        )
    with tenant_context(other_tenant.pk):
        DocumentCategory.objects.create(
            tenant=other_tenant, code="OTHER_ONLY", name="Only the other tenant's"
        )

    with tenant_context(tenant.pk):
        codes = set(DocumentCategory.objects.values_list("code", flat=True))

    assert "HOUSE_RULES" in codes
    assert "ID_COPY" in codes  # shared, seeded
    assert "OTHER_ONLY" not in codes


def test_a_tenant_can_rename_and_delete_a_category_of_its_own(tenant):
    """The D-134 case: is_system exists from this table's first migration, so
    an ordinary UPDATE and DELETE on a tenant's own row must simply work.
    """
    with tenant_context(tenant.pk):
        category = DocumentCategory.objects.create(
            tenant=tenant, code="HOUSE_RULES", name="Signed house rules"
        )

        category.name = "House rules acknowledgement"
        category.save()
        category.refresh_from_db()
        assert category.name == "House rules acknowledgement"

        category.delete()
        assert not DocumentCategory.objects.filter(pk=category.pk).exists()


def test_a_tenant_cannot_edit_a_system_category(tenant, seeded):
    id_copy = DocumentCategory.objects.get(code="ID_COPY")

    with tenant_context(tenant.pk), pytest.raises(DatabaseError) as raised, transaction.atomic():
        DocumentCategory.all_tenants.filter(pk=id_copy.pk).update(name="Renamed")

    assert "system row" in str(raised.value)


def test_a_tenant_cannot_delete_a_system_category(tenant, seeded):
    """DELETE is checked against the policy's USING clause only (D-93) — the
    trigger is the layer that actually holds.
    """
    id_copy = DocumentCategory.objects.get(code="ID_COPY")

    with tenant_context(tenant.pk), pytest.raises(DatabaseError) as raised, transaction.atomic():
        DocumentCategory.all_tenants.filter(pk=id_copy.pk).delete()

    assert "system row" in str(raised.value)


def test_a_tenant_cannot_mint_a_shared_category(tenant):
    """WITH CHECK on enable_rls_shared() lets a tenant write only its own rows —
    never a NULL-tenant one, even from inside its own session.
    """
    with tenant_context(tenant.pk), pytest.raises(DatabaseError) as raised, transaction.atomic():
        DocumentCategory.objects.create(tenant=None, code="SNEAKY", name="Should not land")

    assert "row-level security policy" in str(raised.value)


def test_a_category_requires_a_known_applies_to(tenant):
    with tenant_context(tenant.pk):
        category = DocumentCategory(tenant=tenant, code="BAD", name="Bad", applies_to="planet")
        with pytest.raises(ValidationError) as raised:
            category.full_clean()

    assert "applies_to" in str(raised.value)
