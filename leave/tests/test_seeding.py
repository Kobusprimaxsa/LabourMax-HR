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

import pytest
from django.core.management import call_command

from core.managers import platform_context
from leave.models import LeaveType
from leave.types import SYSTEM_LEAVE_TYPES, seed_system_leave_types

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
    call_command("seedleavetypes", verbosity=0)

    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == len(SYSTEM_LEAVE_TYPES)

    call_command("seedleavetypes", verbosity=0)
    with platform_context():
        assert LeaveType.objects.filter(tenant__isnull=True).count() == len(SYSTEM_LEAVE_TYPES)
