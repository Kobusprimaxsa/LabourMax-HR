"""The employer, and the four states its constraints refuse to let it be in.

Row-level security on these three tables is proved by the generated suite in
``core/tests/test_tenant_isolation.py``, which discovers them from the model registry
— nothing about isolation is repeated here. What is here is the behaviour a scoped
model still has to get right on its own.
"""

from __future__ import annotations

import datetime

import pytest
from django.db import IntegrityError, transaction

from core.managers import tenant_context
from core.models import Tenant
from employers.models import Employer, EmployerStatutoryRegistration
from statutory.models import Sector

MARCH_2026 = datetime.date(2026, 3, 1)
MARCH_2027 = datetime.date(2027, 3, 1)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="A subscriber")


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(trading_name="Another subscriber")


@pytest.fixture
def domestic(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


def employer(tenant, sector, name="A Household", **overrides):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name=name, sector=sector, **overrides)


def registration(tenant, emp, *, kind, frm=MARCH_2026, to=None, **overrides):
    with tenant_context(tenant.pk):
        return EmployerStatutoryRegistration.objects.create(
            tenant=tenant,
            employer=emp,
            registration_type=kind,
            registered_from=frm,
            registered_to=to,
            **overrides,
        )


# ------------------------------------------------------------------- employer


@pytest.mark.django_db
def test_an_employer_belongs_to_a_tenant_and_carries_a_sector(tenant, domestic):
    created = employer(tenant, domestic)
    assert created.tenant == tenant
    assert created.sector == domestic
    assert created.public_uid, "Anything reachable by URL needs a public_uid, never the id."


@pytest.mark.django_db
def test_two_employers_in_one_tenant_cannot_share_a_name(tenant, domestic):
    employer(tenant, domestic, name="Same Name")
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        Employer.objects.create(tenant=tenant, trading_name="Same Name", sector=domestic)


@pytest.mark.django_db
def test_two_tenants_may_each_have_an_employer_of_the_same_name(tenant, other_tenant, domestic):
    """The constraint is per tenant. Two unrelated households both called 'Home'
    is the ordinary case, not a conflict."""
    assert employer(tenant, domestic, name="Home").pk
    assert employer(other_tenant, domestic, name="Home").pk


@pytest.mark.django_db
def test_an_employer_cannot_be_inactive_without_a_deactivation_date(tenant, domestic):
    """Soft delete means the date IS the record. is_active alone loses when it happened,
    and a retention purge five years out needs the date."""
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        Employer.objects.create(
            tenant=tenant, trading_name="Gone", sector=domestic, is_active=False
        )


@pytest.mark.django_db
def test_an_employer_cannot_be_active_and_carry_a_deactivation_date(tenant, domestic):
    from django.utils import timezone

    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        Employer.objects.create(
            tenant=tenant,
            trading_name="Confused",
            sector=domestic,
            is_active=True,
            deactivated_at=timezone.now(),
        )


@pytest.mark.django_db
def test_deleting_a_sector_an_employer_uses_is_refused_by_the_database(tenant, domestic):
    """Three layers of protection, and only the third one actually fires.

    This test was written expecting ``ProtectedError`` from ``on_delete=PROTECT``.
    It got a successful delete and an orphaned employer row, because:

    - Django's collector queries ``employer WHERE sector_id = X`` to find rows to
      protect, and row-level security returns none, so it protects nothing.
    - PostgreSQL's own foreign key check is subject to the policy as well, because
      ``employer`` has FORCE ROW LEVEL SECURITY.
    - ``platform_context()`` does not rescue it either: strict tenant tables carry no
      platform override (D-54).

    So the only layer that can refuse is a trigger on the referenced table, which
    runs regardless of what the deleting session can see. That is what raises here.
    """
    from django.db import DatabaseError

    employer(tenant, domestic)

    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        domestic.delete()

    assert "cannot be deleted from" in str(caught.value)


@pytest.mark.django_db
def test_the_employer_still_points_at_its_sector_afterwards(tenant, domestic):
    """The orphan this guards against. Without the trigger the employer row survives
    with a sector_id referencing a row that no longer exists."""
    from django.db import DatabaseError

    created = employer(tenant, domestic)

    with pytest.raises(DatabaseError), transaction.atomic():
        domestic.delete()

    with tenant_context(tenant.pk):
        reloaded = Employer.objects.get(pk=created.pk)
    assert reloaded.sector_id == domestic.pk


# -------------------------------------------------------------- registrations


@pytest.mark.django_db
def test_an_employer_has_at_most_one_live_registration_per_authority(tenant, domestic):
    emp = employer(tenant, domestic)
    registration(tenant, emp, kind=EmployerStatutoryRegistration.RegistrationType.PAYE)

    with pytest.raises(IntegrityError), transaction.atomic():
        registration(
            tenant,
            emp,
            kind=EmployerStatutoryRegistration.RegistrationType.PAYE,
            frm=MARCH_2027,
        )


@pytest.mark.django_db
def test_a_closed_registration_leaves_room_for_a_new_one(tenant, domestic):
    """The partial unique is on the OPEN rows. An employer who re-registers with a new
    reference number keeps the old one for the periods it applied to."""
    emp = employer(tenant, domestic)
    registration(
        tenant,
        emp,
        kind=EmployerStatutoryRegistration.RegistrationType.PAYE,
        frm=MARCH_2026,
        to=MARCH_2027,
    )
    assert registration(
        tenant,
        emp,
        kind=EmployerStatutoryRegistration.RegistrationType.PAYE,
        frm=MARCH_2027,
    ).pk


@pytest.mark.django_db
def test_different_authorities_are_different_registrations(tenant, domestic):
    emp = employer(tenant, domestic)
    for kind in (
        EmployerStatutoryRegistration.RegistrationType.PAYE,
        EmployerStatutoryRegistration.RegistrationType.SDL,
        EmployerStatutoryRegistration.RegistrationType.COIDA,
    ):
        assert registration(tenant, emp, kind=kind).pk


@pytest.mark.django_db
def test_a_registration_cannot_end_before_it_starts(tenant, domestic):
    emp = employer(tenant, domestic)
    with pytest.raises(IntegrityError), transaction.atomic():
        registration(
            tenant,
            emp,
            kind=EmployerStatutoryRegistration.RegistrationType.UIF_SARS,
            frm=MARCH_2027,
            to=MARCH_2026,
        )


@pytest.mark.django_db
def test_claiming_the_sdl_exemption_requires_saying_why(tenant, domestic):
    """The exemption is forward-looking and human-set — it asks what the employer
    believes about the NEXT twelve months. An unexplained tick is not a record of
    that belief, it is a box somebody clicked."""
    emp = employer(tenant, domestic)
    with pytest.raises(IntegrityError), transaction.atomic():
        registration(
            tenant,
            emp,
            kind=EmployerStatutoryRegistration.RegistrationType.SDL,
            is_exempt=True,
        )


@pytest.mark.django_db
def test_the_sdl_exemption_with_a_reason_is_allowed(tenant, domestic):
    emp = employer(tenant, domestic)
    row = registration(
        tenant,
        emp,
        kind=EmployerStatutoryRegistration.RegistrationType.SDL,
        is_exempt=True,
        exemption_reason="Household with one employee; annual payroll well under the threshold.",
    )
    assert row.is_exempt is True
