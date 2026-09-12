"""Tenant scoping — layer 1 of three.

The other two layers are the PostgreSQL row-level security policies in
``core/db/rls.py`` and the generated isolation suite in
``core/tests/test_tenant_isolation.py``. All three, always. See CLAUDE.md.

Two scoping patterns exist, and the difference is deliberate:

``TenantScopedModel``
    Employer and employee data. ``tenant_id`` is NOT NULL. There is no context
    in which a row without a tenant makes sense.

``TenantOptionalModel``
    Security and operations records that legitimately span the platform and a
    tenant — an OTP issued before the user has picked a tenant, a login attempt
    that failed before we knew who it was, a platform maintenance job.
    ``tenant_id`` is nullable, and the rules are:

    - a tenant session sees only its own rows, never the NULL-tenant ones
    - an anonymous session (no tenant pinned) sees only the NULL-tenant rows
    - the platform console sees everything, and only inside ``platform_context()``

There is exactly one way to read across tenants deliberately, and it is named so
that it shows up in review and in ``grep``.
"""

from __future__ import annotations

import contextvars
import logging

from django.db import connection, models

logger = logging.getLogger(__name__)

# Set by TenantContextMiddleware for the duration of a request, and by
# ``tenant_context()`` inside management commands and Celery tasks.
_current_tenant_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_tenant_id", default=None
)

# Set ONLY by the platform console and by explicit maintenance jobs. Anything
# that sets this is granting itself cross-tenant visibility, so every call site
# must be justified in review.
_platform_access: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "platform_access", default=False
)

TENANT_SESSION_VAR = "labourmax.tenant_id"
PLATFORM_SESSION_VAR = "labourmax.platform_access"


def get_current_tenant_id() -> int | None:
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: int | None):
    """Returns the token needed to reset. Prefer ``tenant_context()``."""
    return _current_tenant_id.set(tenant_id)


def platform_access_enabled() -> bool:
    return _platform_access.get()


class TenantContextError(RuntimeError):
    """Raised when tenant-scoped data is touched with no tenant in context.

    Failing loudly is the point. A silent unscoped query is how one employer
    ends up seeing another employer's employees.
    """


def apply_session_variables(tenant_id: int | None, platform_access: bool = False) -> None:
    """Push the current context into the database session for the RLS policies.

    Layer 1 (managers) and layer 2 (RLS) must be told the same thing, or the
    structural tests pass while the protection is inert. ``set_config(..., true)``
    is transaction-local, so a pooled connection cannot carry one request's
    tenant into the next.

    Best-effort by design: called from ``tenant_context()``, which is used in
    tests and management commands that may have no database at all.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true), set_config(%s, %s, true)",
                [
                    TENANT_SESSION_VAR,
                    str(tenant_id) if tenant_id is not None else "",
                    PLATFORM_SESSION_VAR,
                    "on" if platform_access else "off",
                ],
            )
    except Exception:  # pragma: no cover - no database, or not PostgreSQL
        logger.debug("Could not apply tenant session variables", exc_info=True)


class _ContextBlock:
    """Pins tenant and platform-access for a block, in context AND the database."""

    def __init__(self, tenant_id: int | None, platform_access: bool = False):
        self._tenant_id = tenant_id
        self._platform_access = platform_access
        self._tenant_token = None
        self._platform_token = None

    def __enter__(self):
        self._tenant_token = _current_tenant_id.set(self._tenant_id)
        self._platform_token = _platform_access.set(self._platform_access)
        apply_session_variables(self._tenant_id, self._platform_access)
        return self

    def __exit__(self, *exc):
        _current_tenant_id.reset(self._tenant_token)
        _platform_access.reset(self._platform_token)
        apply_session_variables(get_current_tenant_id(), platform_access_enabled())
        return False


def tenant_context(tenant_id: int | None) -> _ContextBlock:
    """Pin a tenant for a block of work.

    Use in management commands, Celery tasks and tests — anywhere there is no
    request to carry the tenant.
    """
    return _ContextBlock(tenant_id, platform_access=False)


def tenant_context_of(instance) -> _ContextBlock:
    """Pin the tenant that owns this row, for a block of work on it.

    Use this in every service function that reads or writes a tenant-scoped row
    it was handed. Without it the query runs with no tenant pinned, the FORCED
    row-level security policy matches nothing, and what happens next depends
    entirely on how the caller asked:

    - a ``count()`` returns 0, which reads as "none" rather than "not allowed"
    - a ``save(update_fields=...)`` raises "Save with update_fields did not
      affect any rows", because Django checks the affected count
    - a plain ``save()`` silently writes nothing at all

    Only the middle one tells you what is wrong. That is the argument for pinning
    deliberately rather than relying on the caller to already be in context.
    """
    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is None:
        raise TenantContextError(
            f"{type(instance).__name__} has no tenant_id, so its tenant cannot be "
            f"pinned. An unsaved instance, or a tenant-optional model - those "
            f"should use tenant_context() explicitly."
        )
    return _ContextBlock(tenant_id, platform_access=False)


def platform_context() -> _ContextBlock:
    """Grant cross-tenant visibility for a block of work.

    The Labourmax superuser console and a small number of maintenance jobs only.
    This is the one deliberate hole in tenant isolation: it lifts both the
    manager filter and the row-level security policy at the same time, which is
    precisely why it is a single, named, greppable function rather than a flag
    threaded through call sites.

    Every use in employer-facing code is a bug.
    """
    logger.info("platform_context entered - cross-tenant visibility granted")
    return _ContextBlock(None, platform_access=True)


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
    """Default manager for tables whose ``tenant_id`` is NOT NULL.

    ``Model.objects`` is scoped. ``Model.all_tenants`` is not, and exists so the
    platform console and maintenance jobs have an honest, greppable escape hatch.
    """

    def get_queryset(self) -> TenantScopedQuerySet:
        qs = super().get_queryset()
        if platform_access_enabled():
            return qs
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            # No tenant pinned: return nothing rather than everything.
            # Empty results are a visible bug; leaked rows are an invisible one.
            return qs.none()
        return qs.filter(tenant_id=tenant_id)


class TenantOptionalManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Default manager for tables whose ``tenant_id`` is nullable.

    Mirrors ``enable_rls_optional()`` exactly. If you change one, change the
    other, or layer 1 and layer 2 will disagree — and the disagreement will
    surface as a confusing empty result rather than as a test failure.
    """

    def get_queryset(self) -> TenantScopedQuerySet:
        qs = super().get_queryset()
        if platform_access_enabled():
            return qs
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            # Anonymous / pre-tenant context: the platform-level rows only.
            # Registration and login write and read these before any tenant exists.
            return qs.filter(tenant_id__isnull=True)
        # A tenant never sees the platform's rows, only its own.
        return qs.filter(tenant_id=tenant_id)


class AllTenantsManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Unscoped at the ORM level. Named so it is obvious in review and greppable.

    Note that this bypasses layer 1 only. Row-level security still applies, so
    outside ``platform_context()`` this returns the same rows as ``objects``.
    That is the intended behaviour, and the isolation suite asserts it.
    """
