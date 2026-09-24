"""Per-request context: which tenant, and who is acting."""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.audit import audit_actor
from core.managers import (
    _current_tenant_id,
    apply_session_variables,
    set_current_tenant_id,
    tenant_context,
)


class TenantContextMiddleware:
    """Pins the tenant into the request context AND the database session.

    The context variable feeds ``TenantScopedManager`` and
    ``TenantOptionalManager`` (layer 1). The database session variables feed the
    row-level security policies (layer 2), which catch raw SQL, bypassed
    managers and management commands.

    **The pin is set INSIDE a transaction that spans the view** (D-295). It used
    to be set here in autocommit and relied on ``ATOMIC_REQUESTS`` — but
    ``ATOMIC_REQUESTS`` wraps the VIEW, not the middleware, so the
    transaction-local ``set_config`` committed on its own and was gone before
    the view's transaction opened. Every tenant table then read as EMPTY in a
    real request — the D-92 trap one layer up — while every test passed,
    because pytest-django holds one transaction open around each test.
    ``core/tests/test_request_context.py`` runs with ``transaction=True`` and
    asks the database, from inside a view, what it has pinned.

    **The session's tenant is checked against a LIVE membership on every
    request** (D-295). The session value is server-side and set only at login,
    but a membership revoked mid-session, or expired, must stop the next
    request — so the tenant is pinned only for a user who still belongs to it,
    and a session naming any other tenant is cleared and pins nothing.

    A request never gets platform access. Cross-tenant visibility is granted
    only inside ``core.managers.platform_context()``, which the superuser console
    enters explicitly, so an ordinary employer request has no code path that
    could switch it on.

    Must run after AuthenticationMiddleware.
    """

    SESSION_KEY = "active_tenant_id"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id, membership = self._resolve(request)
        request.tenant_id = tenant_id
        request.membership = membership
        token = set_current_tenant_id(tenant_id)
        try:
            if tenant_id is None:
                apply_session_variables(None, platform_access=False)
                return self.get_response(request)
            # atomic FIRST, then the pin (D-92): the set_config is scoped to
            # this transaction, and the view's own ATOMIC_REQUESTS block runs
            # inside it as a savepoint, so the pin holds for all of it.
            with transaction.atomic():
                apply_session_variables(tenant_id, platform_access=False)
                return self.get_response(request)
        finally:
            # Reset both, in case a pooled connection is reused. set_config's
            # transaction-local scope already covers the normal path; this is the
            # belt to its braces.
            apply_session_variables(None, platform_access=False)
            _current_tenant_id.reset(token)

    def _resolve(self, request):
        """The active tenant for this session, and the live membership that
        entitles this user to it — or (None, None).

        A user may hold memberships in several tenants (decision D-04) — a
        bookkeeper serving multiple households. The chosen tenant is stored in
        the session at login (``core/views.py``).
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None, None
        tenant_id = request.session.get(self.SESSION_KEY)
        if tenant_id is None:
            return None, None
        membership = live_membership(user, tenant_id)
        if membership is None:
            request.session.pop(self.SESSION_KEY, None)
            return None, None
        return tenant_id, membership


def live_membership(user, tenant_id):
    """The user's live membership in one tenant, read INSIDE that tenant — the
    table is under FORCE RLS, so an unpinned read would find nothing and look
    exactly like "not a member" (CLAUDE.md's table)."""
    from core.models import TenantMembership

    now = timezone.now()
    with transaction.atomic(), tenant_context(tenant_id):
        return (
            TenantMembership.objects.filter(user=user, is_active=True, revoked_at__isnull=True)
            .filter(Q(access_expires_at__isnull=True) | Q(access_expires_at__gt=now))
            .select_related("tenant")
            .first()
        )


class AuditContextMiddleware:
    """Records who is acting, from where, under which request.

    Separate from TenantContextMiddleware on purpose: that one is a security
    control and this one is a record-keeping control. Merging them would mean a
    change to either risks the other.

    Must run after AuthenticationMiddleware. Without this middleware every change
    is attributed to "system" — accurate, but useless in a dispute.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        authenticated = user is not None and user.is_authenticated

        with audit_actor(
            user_id=user.pk if authenticated else None,
            kind="user" if authenticated else "system",
            impersonated_by_user_id=request.session.get("impersonated_by_user_id")
            if authenticated
            else None,
            ip_address=self._client_ip(request),
            request_id=uuid.uuid4(),
        ):
            return self.get_response(request)

    @staticmethod
    def _client_ip(request):
        """The client address, trusting X-Forwarded-For only behind our own proxy.

        ``USE_X_FORWARDED_FOR`` must stay False unless the deployment actually
        sits behind a proxy that overwrites the header, because a client can
        otherwise put anything it likes in it and choose what the audit trail
        records about itself.
        """
        from django.conf import settings

        if getattr(settings, "USE_X_FORWARDED_FOR", False):
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR")
