"""The tenant is pinned for the WHOLE request, in the transaction the view runs in.

``transaction=True`` on purpose. pytest-django's default wrapper holds one
transaction open around the whole test, which is exactly what hides a
transaction-local ``set_config`` that expired before the view ran — the D-92
trap, one layer up, in the request path itself.
"""

from __future__ import annotations

import pytest
from django.test import Client

from core.managers import tenant_context
from core.models import AppUser, Tenant, TenantMembership
from employers.models import Employer
from statutory.models import Sector

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.urls("core.tests.probe_urls")]


def an_employer_user(name):
    tenant = Tenant.objects.create(trading_name=name)
    sector = Sector.objects.get_or_create(
        code=Sector.Code.DOMESTIC, defaults={"name": "Domestic worker sector"}
    )[0]
    user = AppUser.objects.create_user(email=f"{name.lower()}@example.com", password="x" * 16)
    from django.db import transaction

    with transaction.atomic(), tenant_context(tenant.pk):
        Employer.objects.create(tenant=tenant, trading_name=name, sector=sector)
        TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    return tenant, user


def signed_in(user, tenant) -> Client:
    client = Client()
    client.force_login(user)
    session = client.session
    session["active_tenant_id"] = tenant.pk
    session.save()
    return client


def test_the_database_session_holds_the_tenant_while_the_view_runs():
    tenant, user = an_employer_user("Household")

    seen = signed_in(user, tenant).get("/probe/").json()

    assert seen["pinned"] == str(tenant.pk), "RLS saw no tenant during the view"
    assert seen["request_tenant_id"] == tenant.pk
    assert seen["employers_by_sql"] == 1, "a pinned session sees its own employer"
    assert seen["employers_by_orm"] == 1


def test_an_anonymous_request_pins_nothing_and_sees_nothing():
    an_employer_user("Household")

    seen = Client().get("/probe/").json()

    assert seen["pinned"] == ""
    assert (seen["employers_by_sql"], seen["employers_by_orm"]) == (0, 0)


def test_a_session_naming_a_tenant_the_user_does_not_belong_to_pins_nothing():
    """The classic break: a valid session plus somebody else's tenant id. The
    session value is server-side, but it must still be checked against a live
    membership on every request — a membership revoked mid-session included."""
    _, user = an_employer_user("Household")
    other, _ = an_employer_user("Neighbour")

    seen = signed_in(user, other).get("/probe/").json()

    assert seen["pinned"] == ""
    assert seen["request_tenant_id"] is None
    assert seen["employers_by_sql"] == 0


def test_a_revoked_membership_stops_pinning_on_the_next_request():
    from django.db import transaction
    from django.utils import timezone

    tenant, user = an_employer_user("Household")
    client = signed_in(user, tenant)
    assert client.get("/probe/").json()["pinned"] == str(tenant.pk)

    with transaction.atomic(), tenant_context(tenant.pk):
        TenantMembership.objects.filter(user=user).update(revoked_at=timezone.now())

    assert client.get("/probe/").json()["pinned"] == ""
