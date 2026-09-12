"""Resolves the tenant for a request and pins it in two places at once."""

from __future__ import annotations

from django.db import connection

from core.managers import _current_tenant_id, set_current_tenant_id


class TenantContextMiddleware:
    """Pins the tenant into the request context AND the database session.

    The context variable feeds ``TenantScopedManager`` (layer 1). The database
    session variable feeds the row-level security policies (layer 2), which
    catch raw SQL, bypassed managers and management commands.

    Must run after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = self._resolve(request)
        token = set_current_tenant_id(tenant_id)
        try:
            self._apply_to_database_session(tenant_id)
            request.tenant_id = tenant_id
            return self.get_response(request)
        finally:
            self._apply_to_database_session(None)
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

    @staticmethod
    def _apply_to_database_session(tenant_id):
        """Set the session variable the RLS policies read.

        ``set_config(..., true)`` scopes it to the transaction, so a pooled
        connection can never carry one request's tenant into the next.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('labourmax.tenant_id', %s, true)",
                [str(tenant_id) if tenant_id is not None else ""],
            )
