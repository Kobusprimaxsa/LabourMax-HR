"""Tenant scoping — layer 1 of three.

The other two layers are the PostgreSQL row-level security policies in
``core/db/rls.py`` and the generated isolation suite in
``core/tests/test_tenant_isolation.py``. All three, always. See CLAUDE.md.
"""

from __future__ import annotations

import contextvars

from django.db import models

# Set by TenantContextMiddleware for the duration of a request, and by
# ``tenant_context()`` inside management commands and Celery tasks.
_current_tenant_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_tenant_id", default=None
)


def get_current_tenant_id() -> int | None:
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: int | None):
    """Returns the token needed to reset. Prefer ``tenant_context()``."""
    return _current_tenant_id.set(tenant_id)


class TenantContextError(RuntimeError):
    """Raised when tenant-scoped data is touched with no tenant in context.

    Failing loudly is the point. A silent unscoped query is how one employer
    ends up seeing another employer's employees.
    """


class contextlib_tenant:  # noqa: N801 — used as ``with tenant_context(id):``
    def __init__(self, tenant_id: int | None):
        self._tenant_id = tenant_id
        self._token = None

    def __enter__(self):
        self._token = _current_tenant_id.set(self._tenant_id)
        return self

    def __exit__(self, *exc):
        _current_tenant_id.reset(self._token)
        return False


def tenant_context(tenant_id: int | None) -> contextlib_tenant:
    """Pin a tenant for a block of work.

    Use in management commands, Celery tasks and tests — anywhere there is no
    request to carry the tenant.
    """
    return contextlib_tenant(tenant_id)


class TenantScopedQuerySet(models.QuerySet):
    def for_tenant(self, tenant_id: int) -> TenantScopedQuerySet:
        return self.filter(tenant_id=tenant_id)

    def across_all_tenants(self) -> TenantScopedQuerySet:
        """Deliberately verbose. Platform console and maintenance jobs only.

        Every call site must be justified in review. If you are reaching for
        this inside employer-facing code, you have taken a wrong turn.
        """
        return self


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Default manager that will not return another tenant's rows.

    ``Model.objects`` is scoped. ``Model.all_tenants`` is not, and exists so the
    platform console and maintenance jobs have an honest, greppable escape hatch.
    """

    def get_queryset(self) -> TenantScopedQuerySet:
        qs = super().get_queryset()
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            # No tenant pinned: return nothing rather than everything.
            # Empty results are a visible bug; leaked rows are an invisible one.
            return qs.none()
        return qs.filter(tenant_id=tenant_id)


class AllTenantsManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Unscoped. Named so it is obvious in code review and easy to grep for."""
