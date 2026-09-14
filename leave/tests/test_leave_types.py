"""``leave_type`` as a shared catalogue: who may read it, and who may change it.

This file was written once before, in the container, and never reached the repo —
so the table shipped with the tests for its *shape* (they live beside the
entitlement they serve) and none for its *tenancy*. The gap was not academic. The
migration installs ``lock_system_rows("leave_type")``, a trigger whose first
statement reads ``OLD.is_system``, and the column did not exist: every UPDATE and
DELETE on every row of this table failed inside the trigger. An employer could add
"Birthday leave" and then never rename it.

The one test that touched the area asserted ``DatabaseError`` and passed, because
``record "old" has no field "is_system"`` is a ``DatabaseError`` too. That is the
argument for asserting on the message as well as the class, and every refusal here
does.

The rules under test are D-87's third tenancy base and D-93's system-row lock:

- a tenant reads the catalogue **and** its own rows
- a tenant writes only its own
- a shared row is a system row and nobody edits or deletes it without deliberate
  platform maintenance
- two tenants may each define the same code, and neither sees the other's
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction

from core.db.rls import REFERENCE_MAINTENANCE_VAR
from core.managers import platform_context, tenant_context
from core.models import Tenant
from leave.models import LeaveType

pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="One")


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(trading_name="Two")


@pytest.fixture
def annual(db):
    """The catalogue row, as the platform stocks it."""
    with platform_context():
        return LeaveType.objects.create(
            code=LeaveType.Code.ANNUAL,
            name="Annual leave",
            payable_on_termination=True,
            is_system=True,
        )


def add_own(tenant, code=LeaveType.Code.STUDY, name="Study leave", **overrides):
    with tenant_context(tenant.pk):
        return LeaveType.objects.create(tenant=tenant, code=code, name=name, **overrides)


# ------------------------------------------------------------------ what is read


def test_a_tenant_reads_the_catalogue_and_its_own(tenant, annual):
    mine = add_own(tenant)

    with tenant_context(tenant.pk):
        visible = set(LeaveType.objects.values_list("code", flat=True))

    assert visible == {annual.code, mine.code}


def test_a_tenant_does_not_read_another_tenants_own(tenant, other_tenant, annual):
    add_own(other_tenant, code=LeaveType.Code.STUDY, name="Their study leave")

    with tenant_context(tenant.pk):
        visible = set(LeaveType.objects.values_list("code", flat=True))

    assert visible == {annual.code}, "The catalogue, and nothing of the other tenant's."


def test_raw_sql_from_a_tenant_session_sees_the_same_rows(tenant, other_tenant, annual):
    """The policy, not the manager. A raw cursor is subject to RLS identically —
    which is also why an unpinned raw read looks like an empty table."""
    add_own(other_tenant, name="Their study leave")
    mine = add_own(tenant)

    with tenant_context(tenant.pk), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM leave_type ORDER BY id")
        ids = [row[0] for row in cursor.fetchall()]

    assert ids == sorted([annual.pk, mine.pk])


def test_two_tenants_may_both_define_the_same_code(tenant, other_tenant, annual):
    """Scope is part of the key: COALESCE(tenant_id, 0) + code (D-127)."""
    mine = add_own(tenant, code="BIRTHDAY", name="Birthday leave")
    theirs = add_own(other_tenant, code="BIRTHDAY", name="Birthday")

    assert mine.pk != theirs.pk


def test_the_catalogue_still_cannot_hold_two_of_one_code(annual):
    """NULL = NULL is unknown, so a plain unique would have permitted it."""
    with platform_context(), pytest.raises(IntegrityError), transaction.atomic():
        LeaveType.objects.create(
            code=LeaveType.Code.ANNUAL, name="Annual leave again", is_system=True
        )


# --------------------------------------------------------------- what is written


def test_a_tenant_may_edit_and_delete_its_own(tenant, annual):
    """THE ONE THE MISSING COLUMN BROKE.

    Adding a leave type an employer can never rename or remove is worse than not
    letting them add one — and the trigger refused both for every row, not only
    the shared ones.
    """
    mine = add_own(tenant, code="BIRTHDAY", name="Birthday leave")

    with tenant_context(tenant.pk):
        mine.name = "Birthday day"
        mine.save(update_fields=["name", "updated_at"])
        mine.refresh_from_db()
        assert mine.name == "Birthday day"

        mine.delete()
        assert LeaveType.objects.filter(code="BIRTHDAY").count() == 0


def test_a_tenant_cannot_mint_a_shared_type(tenant):
    """A row with no tenant is visible to every employer on the platform."""
    with tenant_context(tenant.pk), pytest.raises(DatabaseError) as raised, transaction.atomic():
        LeaveType.objects.create(code="SABBATICAL", name="Sabbatical", is_system=True)

    assert "row-level security" in str(raised.value).lower()


def test_a_tenant_cannot_mint_its_own_system_row(tenant):
    """It would be a row of theirs that they could then never edit themselves."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic(), tenant_context(tenant.pk):
        LeaveType.objects.create(
            tenant=tenant, code="SABBATICAL", name="Sabbatical", is_system=True
        )

    assert "system_rows_are_shared_rows" in str(raised.value)


def test_a_shared_row_must_be_a_system_row(db):
    """Otherwise it is readable by everyone and deletable by anyone: a DELETE is
    checked against the policy's USING clause only (D-93)."""
    with platform_context(), pytest.raises(IntegrityError) as raised, transaction.atomic():
        LeaveType.objects.create(code="SABBATICAL", name="Sabbatical", is_system=False)

    assert "shared_rows_are_system_rows" in str(raised.value)


def test_a_tenant_cannot_edit_a_system_type(tenant, annual):
    with tenant_context(tenant.pk), pytest.raises(DatabaseError) as raised, transaction.atomic():
        LeaveType.all_tenants.filter(pk=annual.pk).update(name="Our annual leave")

    assert "system row" in str(raised.value)


def test_a_tenant_cannot_delete_a_system_type(tenant, annual):
    """Without the trigger, one employer tidying their leave list takes ANNUAL out
    of every other employer's."""
    with tenant_context(tenant.pk), pytest.raises(DatabaseError) as raised, transaction.atomic():
        LeaveType.all_tenants.filter(pk=annual.pk).delete()

    assert "system row" in str(raised.value)


def test_the_platform_cannot_casually_edit_one_either(annual):
    """The lock binds the table owner too — which a REVOKE against PUBLIC does not.
    Deliberate maintenance says so by name."""
    with platform_context(), pytest.raises(DatabaseError), transaction.atomic():
        LeaveType.all_tenants.filter(pk=annual.pk).update(name="Renamed")

    with platform_context(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        LeaveType.all_tenants.filter(pk=annual.pk).update(name="Annual leave (renamed)")

    with platform_context():
        annual.refresh_from_db()
    assert annual.name == "Annual leave (renamed)"


# ------------------------------------------------------------------ the shape


def test_a_shared_type_may_not_draw_on_a_tenants_row(tenant, annual):
    """The FK would put one employer's private type in everybody's catalogue, where
    no policy is looking — and then PROTECT would stop that employer deleting it,
    for a reason invisible to them."""
    mine = add_own(tenant, code="BIRTHDAY", name="Birthday leave")

    with platform_context():
        shared = LeaveType(
            code="BIRTHDAY_TOPUP",
            name="Birthday top-up",
            parent_leave_type=mine,
            balance_source=LeaveType.BalanceSource.PARENT,
            accrues=False,
            is_system=True,
        )
        with pytest.raises(ValidationError) as raised:
            shared.full_clean()

    assert "one employer's own type" in str(raised.value)


def test_a_colour_must_be_a_hex_triplet(db):
    """It lands in a leave calendar and in a PDF. A value that is not a colour
    renders as whatever the browser guesses in one and as black in the other,
    which reads as a broken document rather than as a bad setting."""
    with platform_context(), pytest.raises(IntegrityError) as raised, transaction.atomic():
        LeaveType.objects.create(
            code="STUDY", name="Study leave", is_system=True, colour_hex="blue"
        )

    assert "colour_is_a_hex_triplet" in str(raised.value)


def test_a_cycle_of_zero_months_is_refused(db):
    """Sick leave runs 36 months and annual 12. Zero is a balance that never opens."""
    with platform_context(), pytest.raises(IntegrityError), transaction.atomic():
        LeaveType.objects.create(code="STUDY", name="Study leave", is_system=True, cycle_months=0)
