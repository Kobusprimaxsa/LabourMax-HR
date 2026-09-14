"""``seeddocumentcategories`` — idempotent, and never updates an existing row."""

from __future__ import annotations

import pytest
from django.core.management import call_command

from core.managers import platform_context
from documents.categories import SYSTEM_CATEGORIES, seed_system_categories
from documents.models import DocumentCategory
from statutory.models import Sector

pytestmark = pytest.mark.django_db


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )


def test_seeding_creates_every_category_exactly_once(cleaning):
    created = seed_system_categories()
    assert len(created) == len(SYSTEM_CATEGORIES)

    with platform_context():
        assert DocumentCategory.objects.shared().count() == len(SYSTEM_CATEGORIES)

    again = seed_system_categories()
    assert again == []


def test_seeding_never_updates_an_existing_row(cleaning):
    seed_system_categories()
    with platform_context():
        before = DocumentCategory.objects.get(code="ID_COPY").updated_at

    seed_system_categories()

    with platform_context():
        after = DocumentCategory.objects.get(code="ID_COPY").updated_at
    assert after == before


@pytest.mark.django_db(transaction=True)
def test_seeding_works_outside_a_wrapping_transaction(django_db_setup):
    """THE ONE THE ORDINARY TEST DATABASE CANNOT SEE.

    Every test above runs inside pytest-django's wrapping transaction, so a
    context block entered at the top of a function is still in force at the
    bottom. A management command has no such wrapper — under autocommit each
    statement is its own transaction, so ``platform_context()`` sets the flag,
    the SELECT that follows commits, and the flag is gone before the INSERT
    that needs it (D-92). ``seed_system_categories()`` wraps its own
    ``transaction.atomic()`` for exactly this reason; this test is what would
    have caught it if it had not.
    """
    Sector.objects.create(code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector")

    call_command("seeddocumentcategories", verbosity=0)

    with platform_context():
        assert DocumentCategory.objects.shared().count() == len(SYSTEM_CATEGORIES)
