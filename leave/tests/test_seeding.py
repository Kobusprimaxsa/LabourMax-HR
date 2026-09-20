"""``seedleavetypes`` — task 1's own idempotency guarantee, proven twice.

Mirrors ``employers/tests/test_payroll_components.py``'s own two-part proof:
an ordinary run under pytest-django's wrapping transaction, and a second run
with ``transaction=True`` so the command executes exactly the way it does in
production — under autocommit, where ``platform_context()``'s
``set_config(..., true)`` is transaction-local (D-92). A whole green suite can
sit on top of a command that cannot run outside that wrapper, which is
precisely what happened to ``seedcomponents`` before it took out a
transaction of its own.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.management import call_command

from core.managers import platform_context
from leave.evidence import SICK_CERTIFICATE_THRESHOLD_PARAMETER, SYSTEM_EVIDENCE_TYPES
from leave.models import LeaveEvidenceType, LeaveType
from leave.types import SYSTEM_LEAVE_TYPES, seed_system_leave_types
from statutory.models import StatutoryParameter

pytestmark = pytest.mark.django_db


def test_seeding_creates_every_system_leave_type():
    created = seed_system_leave_types()

    assert {c.code for c in created} == {spec.code for spec in SYSTEM_LEAVE_TYPES}
    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == len(SYSTEM_LEAVE_TYPES)


def test_seeding_twice_creates_nothing_and_changes_nothing():
    first = seed_system_leave_types()
    assert len(first) == len(SYSTEM_LEAVE_TYPES)

    with platform_context():
        annual = LeaveType.objects.get(code=LeaveType.Code.ANNUAL, tenant__isnull=True)
        original_name = annual.name

    second = seed_system_leave_types()

    assert second == [], "A second seeding run must create nothing."
    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == len(SYSTEM_LEAVE_TYPES)
        annual.refresh_from_db()
    assert annual.name == original_name


def test_command_list_does_not_touch_the_database():
    call_command("seedleavetypes", "--list", verbosity=0)

    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == 0


@pytest.mark.django_db(transaction=True)
def test_seeding_works_outside_a_wrapping_transaction(django_db_setup):
    """THE ONE THE ORDINARY TEST DATABASE CANNOT SEE (see the module docstring
    and ``test_payroll_components.py``'s own version of this test)."""
    StatutoryParameter.objects.create(
        parameter_code=SICK_CERTIFICATE_THRESHOLD_PARAMETER,
        value_numeric=Decimal("2.000000"),
        unit=StatutoryParameter.Unit.DAYS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s23(1)",
    )

    call_command("seedleavetypes", verbosity=0)

    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == len(SYSTEM_LEAVE_TYPES)
    assert LeaveEvidenceType.objects.count() == len(SYSTEM_EVIDENCE_TYPES)

    call_command("seedleavetypes", verbosity=0)
    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == len(SYSTEM_LEAVE_TYPES)
    assert LeaveEvidenceType.objects.count() == len(SYSTEM_EVIDENCE_TYPES)


# ------------------------- the three KwaZulu-Natal types nobody else has (D-268)


@pytest.mark.django_db
@pytest.mark.parametrize("code", ["PRENATAL", "SHOP_STEWARD"])
def test_the_kzn_leave_types_are_seeded_as_shared_system_rows(code):
    """A catalogue row is what an employer picks from; the QUANTUM is
    area-scoped reference data (statutory/tests/test_kzn_leave_types.py). These
    two are new codes; STUDY was already seeded and is tested below."""
    seed_system_leave_types()

    with platform_context():
        row = LeaveType.objects.get(code=code, tenant__isnull=True)

    assert row.is_system is True, "or the first employer to tidy their list deletes it (D-93)"
    assert row.tenant_id is None, "shared, so every tenant sees it (D-87)"
    assert row.is_active is True


@pytest.mark.django_db
@pytest.mark.parametrize("code", ["PRENATAL", "SHOP_STEWARD", "STUDY"])
def test_none_of_the_three_accrues_or_carries_a_balance_this_engine_would_touch(code):
    """NONE of them is a per-cycle bank, and saying so on the row is what keeps
    the accrual engine away: ``accrue_employee`` returns early on
    ``accrues=False``, so nothing is ever granted automatically.

    Study leave is per EXAMINATION, the prenatal day is per PREGNANCY, and shop
    steward leave is per year at one of two figures chosen by a fact about the
    person that no column carries. An annual bank for any of them would be an
    invented shape, and ``ensure_cycles()`` would write a zero entitlement
    against it and mark every day of such leave unpaid.
    """
    seed_system_leave_types()

    with platform_context():
        row = LeaveType.objects.get(code=code, tenant__isnull=True)

    assert row.accrues is False


@pytest.mark.django_db
def test_the_accrual_engine_grants_nothing_for_them(annual_type, employee, engagement):
    """Watched NOT firing, on the real engine rather than by reading the flag."""
    from leave.accrual import accrue_employee

    seed_system_leave_types()
    with platform_context():
        prenatal = LeaveType.objects.get(code="PRENATAL", tenant__isnull=True)

    assert accrue_employee(employee, prenatal, as_at=datetime.date(2026, 4, 30)) is None


@pytest.mark.django_db
def test_prenatal_and_shop_steward_keep_no_balance_at_all():
    """balance_source=none, like UNPAID: there is no bank, so there is nothing
    to exhaust and nothing for a monthly run to add to. The CAP still has to
    live somewhere, and it lives in the resolver — not enforced yet (O-31)."""
    seed_system_leave_types()

    with platform_context():
        for code in ("PRENATAL", "SHOP_STEWARD"):
            assert LeaveType.objects.get(code=code, tenant__isnull=True).balance_source == "none"


@pytest.mark.django_db
def test_study_leaves_is_statutory_flag_is_wrong_for_one_sector_and_cannot_be_fixed():
    """PINNED DELIBERATELY, so the next reader finds the note rather than the
    surprise (O-36). ``STUDY`` was seeded ``is_statutory=False`` — right for
    every employer who grants it as a benefit, wrong for a KwaZulu-Natal
    contract cleaner, whose agreement compels it at clause 12. A system row is
    locked against UPDATE (D-93) and seeding never updates, so the value cannot
    be corrected in place. It is read by no application code, so nothing
    computes a wrong answer from it.
    """
    seed_system_leave_types()

    with platform_context():
        study = LeaveType.objects.get(code="STUDY", tenant__isnull=True)
        prenatal = LeaveType.objects.get(code="PRENATAL", tenant__isnull=True)

    assert study.is_statutory is False
    assert prenatal.is_statutory is True, (
        "a new row is not given a known-wrong value for symmetry with one that "
        "cannot be fixed - the asymmetry is recorded instead (O-36)"
    )
