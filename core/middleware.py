"""Per-request context: which tenant, and who is acting."""

from __future__ import annotations

import uuid

from core.audit import audit_actor
from core.managers import _current_tenant_id, apply_session_variables, set_current_tenant_id


class TenantContextMiddleware:
    """Pins the tenant into the request context AND the database session.

    The context variable feeds ``TenantScopedManager`` and
    ``TenantOptionalManager`` (layer 1). The database session variables feed the
    row-level security policies (layer 2), which catch raw SQL, bypassed
    managers and management commands.

    A request never gets platform access. Cross-tenant visibility is granted
    only inside ``core.managers.platform_context()``, which the superuser console
    enters explicitly, so an ordinary employer request has no code path that
    could switch it on.

    Must run after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = self._resolve(request)
        token = set_current_tenant_id(tenant_id)
        try:
            apply_session_variables(tenant_id, platform_access=False)
            request.tenant_id = tenant_id
            return self.get_response(request)
        finally:
            # Reset both, in case a pooled connection is reused. set_config's
            # transaction-local scope already covers the normal path; this is the
            # belt to its braces.
            apply_session_variables(None, platform_access=False)
            _current_tenant_id.reset(token)

    def _resolve(self, request):
        """The active tenant for this session.

        A user may hold memberships in several tenants (decision D-04) — a
        bookkeeper serving multiple households. The chosen tenant is stored in
        the session by the tenant picker at login.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        return request.session.get("active_tenant_id")


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
