"""An unpaid leave day must not also spend earned leave (D-188 amended, D-193).

Two behaviours are possible for a day that is unpaid in full, and only one is
lawful:

- the day is unpaid, and the ledger deducts NOTHING — the employee keeps the
  leave they had and draws it another day
- the day is unpaid AND the ledger deducts it — the employee is charged earned
  entitlement and paid for none of it. That is a forfeiture of accrued leave,
  and invisible on a payslip: the payslip shows the unpaid day, the balance
  drops on a different screen

Each test asserts BOTH the unpaid figure and the resulting balance, so neither
half can pass alone.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import AppUser, TenantMembership
from employees.models import EmployeeLeaveEntitlement
from leave.applications import submit_application
from leave.authorisation import approve
from leave.balances import recompute_cycle
from leave.cycles import ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveCycle, LeaveTransaction
from leave.tests.conftest import START

pytestmark = pytest.mark.django_db

MONDAY = datetime.date(2026, 3, 2)
FRIDAY = datetime.date(2026, 3, 6)
TransactionType = LeaveTransaction.TransactionType


@pytest.fixture
def owner(tenant):
    user = AppUser.objects.create_user(email="unpaid-owner@example.com", password="x" * 16)
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    return user


def _hold(employee, leave_type, quantity, unit):
    with tenant_context(employee.tenant_id):
        cycle = ensure_cycles(employee, leave_type, horizon=MONDAY)[0]
        assert cycle.unit == unit
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=leave_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal(quantity),
            unit=unit,
            transaction_date=MONDAY,
            calculation_basis="manual",
        )
    return cycle


def _taken(application):
    with tenant_context(application.tenant_id):
        return list(
            LeaveTransaction.objects.filter(
                leave_application=application, transaction_type=TransactionType.TAKEN
            )
        )


def test_an_overdrawn_hours_day_is_unpaid_in_full_and_charges_the_balance_nothing(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    schedule_5day,
    owner,
    working_time_rules,
):
    """Holds 3 hours, applies for an 8-hour day. The whole day falls unpaid
    (the overdraw works a whole day at a time, D-188) — so the 3 hours held
    must still be there afterwards, not spent on a day nobody paid for."""
    with tenant_context(employee.tenant_id):
        EmployeeLeaveEntitlement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            leave_type=annual_type,
            accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
            effective_from=START,
        )
    cycle = _hold(employee, annual_type, "3.000", LeaveCycle.Unit.HOURS)

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=MONDAY
    )
    approve(application, decided_by=owner)

    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
    assert application.unpaid_hours == Decimal("8.000")
    assert _taken(application) == [], "an unpaid day must post no TAKEN row"
    assert balance == Decimal("3.000"), (
        f"the 3 hours held must be retained, got {balance}: an unpaid day that spends "
        f"earned leave is a forfeiture"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D-193, awaiting Kobus: ANNUAL_UNAUTHORISED is seeded is_paid=False AND draws "
        "on the ANNUAL balance (D-127, D-180), so an unauthorised absence is unpaid "
        "AND spends earned annual leave. strict: this fails loudly once fixed."
    ),
)
def test_an_unauthorised_absence_is_not_both_unpaid_and_charged_to_annual_leave(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    annual_unauthorised_type,
    schedule_5day,
    owner,
    working_time_rules,
):
    cycle = _hold(employee, annual_type, "15.000", LeaveCycle.Unit.DAYS)

    application = submit_application(
        employee, leave_type=annual_unauthorised_type, start_date=MONDAY, end_date=MONDAY
    )
    approve(application, decided_by=owner)

    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
    assert application.unpaid_days == Decimal("1.000")
    assert balance == Decimal("15.000"), f"charged AND unpaid: balance {balance}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D-193, awaiting Kobus and O-06: sick leave withheld for want of a certificate "
        "(BCEA s23(1)) is unpaid AND deducted from the sick balance. strict: this fails "
        "loudly once fixed."
    ),
)
def test_uncertified_sick_leave_is_not_both_unpaid_and_charged_to_the_sick_balance(
    employee,
    engagement,
    leave_rules,
    sick_type,
    evidence_types,
    schedule_5day,
    owner,
    working_time_rules,
):
    cycle = _hold(employee, sick_type, "30.000", LeaveCycle.Unit.DAYS)

    application = submit_application(
        employee, leave_type=sick_type, start_date=MONDAY, end_date=FRIDAY
    )
    approve(application, decided_by=owner)

    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
    assert application.unpaid_days == Decimal("5.000")
    assert balance == Decimal("30.000"), f"charged AND unpaid: balance {balance}"
