"""Administrative seat accounting — the application half of the limit.

The database trigger in ``core/db/constraints.py`` is what actually enforces the
limit. This module exists so a user meets a sentence they can act on instead of
a 500, and so a caller can ask "is there room?" before starting a flow it cannot
finish.

Both halves read ``ADMINISTRATIVE_ROLES`` and ``tenant.max_admin_users``, so
they cannot drift apart. If they ever disagree, the database wins and the user
sees a raw error — which is why the test suite asserts they agree.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError

from core.db.constraints import ADMINISTRATIVE_ROLES


class SeatLimitError(ValidationError):
    """Raised before the database has to. Carries a message a user can act on."""


def seats_in_use(tenant_id: int, *, excluding_membership_id: int | None = None) -> int:
    """Count the administrative memberships currently occupying a seat.

    Runs inside ``tenant_context(tenant_id)`` deliberately, and this is not
    optional. ``tenant_membership`` carries a FORCED row-level security policy,
    so a query issued with no tenant pinned returns **zero rows rather than an
    error** — which this function would then report as "no seats in use", and
    ``assert_seat_available`` would cheerfully wave through a fourth admin.

    That is the general trap with row-level security: a missing context looks
    exactly like an empty table. ``all_tenants`` does not help, because it only
    bypasses the manager, not the database policy.

    Entering the context grants nothing extra — it reads one tenant's own rows.
    """
    from core.managers import tenant_context
    from core.models import TenantMembership

    with tenant_context(tenant_id):
        qs = TenantMembership.objects.filter(
            revoked_at__isnull=True,
            role__in=ADMINISTRATIVE_ROLES,
        )
        if excluding_membership_id is not None:
            qs = qs.exclude(pk=excluding_membership_id)
        return qs.count()


def seat_limit(tenant_id: int) -> int:
    from core.models import Tenant

    return Tenant.objects.values_list("max_admin_users", flat=True).get(pk=tenant_id)


def seats_available(tenant_id: int, *, excluding_membership_id: int | None = None) -> int:
    used = seats_in_use(tenant_id, excluding_membership_id=excluding_membership_id)
    return max(0, seat_limit(tenant_id) - used)


def assert_seat_available(
    tenant_id: int, role: str, *, excluding_membership_id: int | None = None
) -> None:
    """Call before creating or reactivating an administrative membership.

    Raises ``SeatLimitError``. Not a substitute for the trigger — two callers
    can pass this check simultaneously and only the trigger's row lock stops
    them both writing.
    """
    if role not in ADMINISTRATIVE_ROLES:
        return

    used = seats_in_use(tenant_id, excluding_membership_id=excluding_membership_id)
    limit = seat_limit(tenant_id)
    if used + 1 > limit:
        raise SeatLimitError(
            f"This account allows {limit} administrative users and {used} are in use. "
            f"Revoke an existing user before adding another, or increase the limit."
        )
