"""Signing in, as a service the views call. Every attempt writes ``login_audit``.

``login_audit`` has existed since P0 and nothing wrote to it before this module
(D-296). Now every attempt writes one row, success and failure both: an unknown
address, a wrong password, an account locked, and a success. The row carries
the address as typed, the user where one matched, the client address and agent,
and a hash of the new session key on success. It belongs to NO tenant: the
tenant is not known until after the password is checked, and an attempt on an
account in several tenants is one attempt, not several.

**Two steps, so a second factor slots in between without a rewrite.**
``check_password()`` decides whether the password is right and writes the
audit row; ``complete_sign_in()`` is what actually signs the user in and picks
the tenant. Today the view calls one straight after the other. An OTP step
(P1, ``otp_challenge``) goes between them: on a user with ``mfa_enabled`` the
view stores the pending user in the session, sends the code, and calls
``complete_sign_in()`` only once the code verifies — writing ``mfa_success`` or
``mfa_failed``, both of which ``LoginAudit.Outcome`` already has.

**Finding a user's tenants reads each tenant's own rows** (O-48). The
membership table is under FORCE RLS and a request may never enter
``platform_context()``, so ``memberships_of()`` pins each tenant in turn and
asks. Correct, and one query per tenant — fine for a pilot, not for a
thousand tenants, and the proper mechanism is a decision for Kobus.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib

from django.conf import settings
from django.contrib.auth import authenticate, login
from django.db import transaction
from django.utils import timezone

from core.managers import tenant_context
from core.middleware import TenantContextMiddleware, live_membership
from core.models import AppUser, LoginAudit, Tenant, TenantMembership

Outcome = LoginAudit.Outcome


@dataclasses.dataclass(frozen=True)
class Attempt:
    user: AppUser | None
    outcome: str

    @property
    def succeeded(self) -> bool:
        return self.outcome == Outcome.SUCCESS


def _client(request) -> dict:
    return {
        "ip_address": request.META.get("REMOTE_ADDR") or None,
        "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:400],
    }


def audit(request, *, outcome: str, email: str, user=None, session_key: str = "") -> LoginAudit:
    """One append-only row. No tenant: see the module docstring."""
    return LoginAudit.objects.create(
        tenant=None,
        user=user,
        email_attempted=email[:254],
        outcome=outcome,
        session_key_hash=hashlib.sha256(session_key.encode()).hexdigest() if session_key else "",
        **_client(request),
    )


def check_password(request, email: str, password: str) -> Attempt:
    """Step one: is this the right password for an account that may sign in?

    The message a person sees is the same for an unknown address and a wrong
    password — telling them apart would tell a stranger which addresses have
    accounts. The AUDIT row tells them apart, because the owner of the account
    is entitled to know.
    """
    email = (email or "").strip()
    user = AppUser.objects.filter(email__iexact=email).first()
    now = timezone.now()

    if user is None:
        audit(request, outcome=Outcome.UNKNOWN_USER, email=email)
        return Attempt(None, Outcome.UNKNOWN_USER)

    if user.locked_until is not None and user.locked_until > now:
        audit(request, outcome=Outcome.LOCKED, email=email, user=user)
        return Attempt(user, Outcome.LOCKED)

    if authenticate(request, email=user.email, password=password) is None:
        user.failed_login_count += 1
        outcome = Outcome.BAD_PASSWORD
        if user.failed_login_count >= settings.LOGIN_FAILURE_LIMIT:
            user.locked_until = now + datetime.timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            user.failed_login_count = 0
            outcome = Outcome.LOCKED
        user.save(update_fields=["failed_login_count", "locked_until", "updated_at"])
        audit(request, outcome=outcome, email=email, user=user)
        return Attempt(user, outcome)

    return Attempt(user, Outcome.SUCCESS)


def complete_sign_in(request, user: AppUser) -> list[TenantMembership]:
    """Step two: sign the user in, write the success row, and choose the tenant
    if there is exactly one. Returns the live memberships found."""
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = timezone.now()
    user.save(update_fields=["failed_login_count", "locked_until", "last_login_at", "updated_at"])
    audit(
        request,
        outcome=Outcome.SUCCESS,
        email=user.email,
        user=user,
        session_key=request.session.session_key or "",
    )
    memberships = memberships_of(user)
    if len(memberships) == 1:
        choose(request, user, memberships[0].tenant_id)
    return memberships


def memberships_of(user: AppUser) -> list[TenantMembership]:
    """Every live membership, each read inside its own tenant (O-48). The last
    tenant used is asked first, because it is the likeliest answer."""
    tenant_ids = list(Tenant.objects.order_by("pk").values_list("pk", flat=True))
    if user.last_tenant_id in tenant_ids:
        tenant_ids.remove(user.last_tenant_id)
        tenant_ids.insert(0, user.last_tenant_id)
    found = []
    for tenant_id in tenant_ids:
        membership = live_membership(user, tenant_id)
        if membership is not None:
            found.append(membership)
    return found


def choose(request, user: AppUser, tenant_id: int) -> bool:
    """Put a tenant in the session — only after proving a live membership in it.
    The one place a tenant id arrives from the browser, and it is never trusted:
    it is checked here, and again by the middleware on every request after."""
    if live_membership(user, tenant_id) is None:
        return False
    request.session[TenantContextMiddleware.SESSION_KEY] = tenant_id
    user.last_tenant_id = tenant_id
    user.save(update_fields=["last_tenant", "updated_at"])
    return True


def tenant_name(tenant_id: int) -> str:
    with transaction.atomic(), tenant_context(tenant_id):
        return Tenant.objects.values_list("trading_name", flat=True).get(pk=tenant_id)
