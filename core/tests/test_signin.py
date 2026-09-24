"""Signing in: every attempt is in ``login_audit``, and a tenant is chosen only
among live memberships (D-296)."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from core.models import AppUser, LoginAudit
from core.tests.web_world import PASSWORD, a_world

pytestmark = pytest.mark.django_db

Outcome = LoginAudit.Outcome


def sign_in(client, email, password=PASSWORD, next_url=""):
    url = reverse("login") + (f"?next={next_url}" if next_url else "")
    return client.post(url, {"email": email, "password": password})


def audits(email=None):
    rows = LoginAudit.objects.order_by("pk")
    if email:
        rows = rows.filter(email_attempted=email)
    return list(rows.values_list("outcome", flat=True))


def test_a_successful_sign_in_is_audited_and_picks_the_only_tenant(db):
    world = a_world("Household")
    client = Client()

    response = sign_in(client, world.user.email)

    assert response.status_code == 302 and response["Location"] == reverse("home")
    assert audits(world.user.email) == [Outcome.SUCCESS]
    row = LoginAudit.objects.get(email_attempted=world.user.email)
    assert row.user_id == world.user.pk and row.tenant_id is None
    assert len(row.session_key_hash) == 64
    assert client.session["active_tenant_id"] == world.tenant.pk
    assert client.get(reverse("home")).status_code == 200


def test_an_unknown_address_is_audited_and_told_the_same_as_a_wrong_password(db):
    world = a_world("Household")

    unknown = sign_in(Client(), "nobody@example.com").content.decode()
    wrong = sign_in(Client(), world.user.email, "not-the-password").content.decode()

    assert audits("nobody@example.com") == [Outcome.UNKNOWN_USER]
    assert audits(world.user.email) == [Outcome.BAD_PASSWORD]
    assert "do not match an account" in unknown and "do not match an account" in wrong


def test_repeated_failures_lock_the_account_even_against_the_right_password(db):
    world = a_world("Household")
    for _ in range(settings.LOGIN_FAILURE_LIMIT):
        sign_in(Client(), world.user.email, "wrong")

    locked_out = sign_in(Client(), world.user.email)  # the RIGHT password

    assert audits(world.user.email)[-2:] == [Outcome.LOCKED, Outcome.LOCKED]
    assert "locked" in locked_out.content.decode()
    world.user.refresh_from_db()
    assert world.user.locked_until is not None


def test_a_user_in_two_tenants_chooses_and_cannot_choose_a_third(db):
    first = a_world("Household")
    second = a_world("Bookkeeper")
    from django.db import transaction

    from core.managers import tenant_context
    from core.models import TenantMembership

    with transaction.atomic(), tenant_context(second.tenant.pk):
        TenantMembership.objects.create(tenant=second.tenant, user=first.user, role="admin")
    outsider = a_world("Stranger")
    client = Client()

    assert sign_in(client, first.user.email)["Location"] == reverse("choose_tenant")
    page = client.get(reverse("choose_tenant")).content.decode()
    assert "Household" in page and "Bookkeeper" in page and "Stranger" not in page

    refused = client.post(reverse("choose_tenant"), {"tenant": str(outsider.tenant.public_uid)})
    assert refused.status_code == 200 and "active_tenant_id" not in client.session

    chosen = client.post(reverse("choose_tenant"), {"tenant": str(second.tenant.public_uid)})
    assert chosen["Location"] == reverse("home")
    assert client.session["active_tenant_id"] == second.tenant.pk


def test_next_may_not_send_a_user_off_site(db):
    world = a_world("Household")

    response = sign_in(Client(), world.user.email, next_url="https://evil.example/")

    assert response["Location"] == reverse("home")


def test_sign_out_is_post_only(db):
    world = a_world("Household")
    client = Client()
    sign_in(client, world.user.email)

    assert client.get(reverse("logout")).status_code == 405
    assert client.post(reverse("logout")).status_code == 302
    assert client.get(reverse("home")).status_code == 302


def test_changing_a_password_records_when(db):
    world = a_world("Household")
    client = Client()
    sign_in(client, world.user.email)

    response = client.post(
        reverse("change_password"),
        {
            "old_password": PASSWORD,
            "new_password1": "another-long-password-9",
            "new_password2": "another-long-password-9",
        },
    )

    assert response.status_code == 302
    user = AppUser.objects.get(pk=world.user.pk)
    assert user.password_changed_at is not None
    assert user.password.startswith("argon2"), "Argon2id, first in PASSWORD_HASHERS"


def test_the_session_and_csrf_cookies_are_httponly_and_samesite(db):
    world = a_world("Household")
    client = Client()
    client.get(reverse("login"))
    response = sign_in(client, world.user.email)

    session = response.cookies[settings.SESSION_COOKIE_NAME]
    assert session["httponly"] is True
    assert session["samesite"] == "Lax"
    assert settings.CSRF_COOKIE_HTTPONLY is True
    assert settings.SESSION_EXPIRE_AT_BROWSER_CLOSE is True


def test_production_marks_both_cookies_secure():
    from labourmax.settings import prod

    assert prod.SESSION_COOKIE_SECURE is True
    assert prod.CSRF_COOKIE_SECURE is True
