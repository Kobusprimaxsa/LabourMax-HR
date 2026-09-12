"""The administrative user limit — "one admin plus exactly one more".

The brief's rule, enforced in the database rather than in a form. These tests
deliberately write through ``objects.create``, which never calls ``clean()``, so
what is being tested is the trigger and not the courtesy check in front of it.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from core.db.constraints import ADMINISTRATIVE_ROLES
from core.managers import tenant_context
from core.models import AppUser, Tenant, TenantMembership
from core.seats import assert_seat_available, seat_limit, seats_available, seats_in_use

Role = TenantMembership.Role

SOURCE_SQL = "SELECT prosrc FROM pg_proc WHERE proname = 'labourmax_enforce_admin_seat_limit'"


def trigger_source():
    with connection.cursor() as cursor:
        cursor.execute(SOURCE_SQL)
        row = cursor.fetchone()
    return row[0] if row else None


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Seat Limit Co")


def make_user(index: int) -> AppUser:
    return AppUser.objects.create_user(
        email=f"seat{index}@example.com",
        password="x",
        mobile_number=f"+278200007{index:02d}",
    )


def add(tenant, index, role=Role.ADMIN):
    with tenant_context(tenant.pk):
        return TenantMembership.objects.create(tenant=tenant, user=make_user(index), role=role)


# ------------------------------------------------------------------ structural


@pytest.mark.isolation
def test_the_trigger_exists_on_the_table(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'tenant_membership'::regclass "
            "AND NOT tgisinternal"
        )
        triggers = [r[0] for r in cursor.fetchall()]
    assert "tenant_membership_admin_seat_limit" in triggers


@pytest.mark.isolation
def test_the_trigger_locks_before_counting(db):
    """Concurrency safety, asserted structurally because a race is not unit-testable.

    Two invitations accepted in the same instant would each count one seat used,
    each find one free, and both insert. ``FOR UPDATE`` on the tenant row is the
    only thing preventing that, and it is one line away from being deleted by
    someone tidying the function.
    """
    source = trigger_source()
    assert source is not None, "The seat-limit function is missing."
    assert "FOR UPDATE" in source, (
        "The seat-limit trigger counts rows without locking the tenant first. "
        "The limit will hold in testing and fail under concurrent invitations."
    )


@pytest.mark.isolation
def test_application_and_database_agree_on_which_roles_count(db):
    """Two halves of one rule. If they drift, users meet raw database errors."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE proname = 'labourmax_enforce_admin_seat_limit'"
        )
        source = cursor.fetchone()[0]
    for role in ADMINISTRATIVE_ROLES:
        assert f"'{role}'" in source, f"{role} counts in Python but not in the trigger."
    assert "'employee'" not in source


# ----------------------------------------------------------------- the limit


@pytest.mark.isolation
def test_two_administrative_users_are_allowed(db, tenant):
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)
    with tenant_context(tenant.pk):
        assert TenantMembership.objects.count() == 2


@pytest.mark.isolation
def test_a_third_administrative_user_is_refused_by_the_database(db, tenant):
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)

    with pytest.raises(DatabaseError, match="administrative user limit reached"):
        with transaction.atomic():
            add(tenant, 3, Role.ADMIN)


@pytest.mark.isolation
def test_read_only_occupies_a_seat(db, tenant):
    """Otherwise the limit is avoidable: invite read-only users and read everything."""
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.READ_ONLY)

    with pytest.raises(DatabaseError, match="administrative user limit reached"):
        with transaction.atomic():
            add(tenant, 3, Role.READ_ONLY)


@pytest.mark.isolation
def test_employee_self_service_logins_do_not_occupy_seats(db, tenant):
    """An employer with forty cleaners is not buying forty seats (D-21)."""
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)

    for i in range(3, 8):
        add(tenant, i, Role.EMPLOYEE)

    with tenant_context(tenant.pk):
        assert TenantMembership.objects.count() == 7
    assert seats_in_use(tenant.pk) == 2


@pytest.mark.isolation
def test_revoking_frees_a_seat(db, tenant):
    """Decision D-06: a role can be re-filled after someone leaves."""
    add(tenant, 1, Role.OWNER)
    second = add(tenant, 2, Role.ADMIN)

    with tenant_context(tenant.pk):
        second.revoked_at = timezone.now()
        second.save()

    assert seats_in_use(tenant.pk) == 1
    replacement = add(tenant, 3, Role.ADMIN)
    assert replacement.pk != second.pk
    assert seats_in_use(tenant.pk) == 2


@pytest.mark.isolation
def test_a_revoked_membership_cannot_be_quietly_reactivated_past_the_limit(db, tenant):
    """The path that a column-list trigger would have missed."""
    add(tenant, 1, Role.OWNER)
    revoked = add(tenant, 2, Role.ADMIN)
    with tenant_context(tenant.pk):
        revoked.revoked_at = timezone.now()
        revoked.save()

    add(tenant, 3, Role.ADMIN)  # takes the freed seat

    with pytest.raises(DatabaseError, match="administrative user limit reached"):
        with transaction.atomic(), tenant_context(tenant.pk):
            revoked.revoked_at = None
            revoked.save()


@pytest.mark.isolation
def test_updating_a_membership_in_place_does_not_count_itself_twice(db, tenant):
    add(tenant, 1, Role.OWNER)
    second = add(tenant, 2, Role.ADMIN)

    with tenant_context(tenant.pk):
        second.is_active = False
        second.save()  # must not raise
        second.refresh_from_db()
        assert second.is_active is False


@pytest.mark.isolation
def test_the_limit_is_data_not_a_constant(db, tenant):
    """An account can be varied without a code change."""
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)

    tenant.max_admin_users = 3
    tenant.save()

    third = add(tenant, 3, Role.ADMIN)
    assert third.pk
    assert seats_available(tenant.pk) == 0


# ------------------------------------------------------- the application half


@pytest.mark.isolation
def test_the_application_check_raises_before_the_database_has_to(db, tenant):
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)

    with pytest.raises(ValidationError, match="administrative users"):
        assert_seat_available(tenant.pk, Role.ADMIN)


@pytest.mark.isolation
def test_the_application_check_ignores_employee_role(db, tenant):
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)
    assert_seat_available(tenant.pk, Role.EMPLOYEE)  # must not raise


@pytest.mark.isolation
def test_clean_surfaces_the_limit_for_forms(db, tenant):
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)

    with tenant_context(tenant.pk):
        candidate = TenantMembership(tenant=tenant, user=make_user(9), role=Role.ADMIN)
        with pytest.raises(ValidationError):
            candidate.clean()


@pytest.mark.isolation
def test_seat_accounting_reports_agree_with_the_limit(db, tenant):
    assert seat_limit(tenant.pk) == 2
    assert seats_in_use(tenant.pk) == 0
    assert seats_available(tenant.pk) == 2

    add(tenant, 1, Role.OWNER)
    assert seats_in_use(tenant.pk) == 1
    assert seats_available(tenant.pk) == 1


@pytest.mark.isolation
def test_seat_counting_works_with_no_tenant_pinned(db, tenant):
    """Regression: row-level security made a missing context look like an empty table.

    ``seats_in_use`` queried ``tenant_membership`` with no tenant pinned. The
    FORCED policy returned zero rows rather than an error, so the count came back
    0, ``assert_seat_available`` never raised, and the friendly check in front of
    the trigger was dead code that always said yes.

    Registration creates the first owner before any tenant is pinned, so this is
    the ordinary path and not an edge case.
    """
    add(tenant, 1, Role.OWNER)
    add(tenant, 2, Role.ADMIN)

    # Deliberately outside any tenant_context.
    assert seats_in_use(tenant.pk) == 2
    assert seats_available(tenant.pk) == 0
    with pytest.raises(ValidationError):
        assert_seat_available(tenant.pk, Role.ADMIN)
