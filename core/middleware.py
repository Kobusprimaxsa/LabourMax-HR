"""Resolves the tenant for a request and pins it in two places at once."""

from __future__ import annotations

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
