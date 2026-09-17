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


def _elect(employee, value):
    from employers.models import EmployerSetting

    with tenant_context(employee.tenant_id):
        EmployerSetting.objects.create(
            tenant=employee.tenant,
            employer=employee.employer,
            setting_key="UNAUTHORISED_ABSENCE_TREATMENT",
            value_type=EmployerSetting.ValueType.TEXT,
            value_text=value,
            set_by_employer=True,
        )


def _unauthorised_day(employee, annual_type, annual_unauthorised_type, owner, held):
    cycle = _hold(employee, annual_type, held, LeaveCycle.Unit.DAYS)
    application = submit_application(
        employee, leave_type=annual_unauthorised_type, start_date=MONDAY, end_date=MONDAY
    )
    approve(application, decided_by=owner)
    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
        day = application.days.get()
    return application, day, balance


def test_by_default_an_unauthorised_absence_is_unpaid_and_charges_nothing(
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
    """D-195, the default election (`unpaid`): no work, no pay — and no earned
    annual leave spent without the employee's agreement. Was D-193's defect:
    unpaid AND charged, balance 14."""
    application, day, balance = _unauthorised_day(
        employee, annual_type, annual_unauthorised_type, owner, "15.000"
    )

    assert day.is_paid is False
    assert day.deducted_from_balance is False
    assert application.unpaid_days == Decimal("1.000")
    assert _taken(application) == [], "unpaid: no TAKEN row"
    assert balance == Decimal("15.000"), f"charged AND unpaid: balance {balance}"


def test_an_employer_may_elect_to_charge_an_unauthorised_absence_to_annual_leave_and_pay_it(
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
    """D-195, `annual_leave`: the day is annual leave — spent from the balance
    AND paid. Never one without the other."""
    _elect(employee, "annual_leave")
    application, day, balance = _unauthorised_day(
        employee, annual_type, annual_unauthorised_type, owner, "15.000"
    )

    assert day.is_paid is True
    assert day.deducted_from_balance is True
    assert application.unpaid_days == Decimal("0")
    assert [t.days for t in _taken(application)] == [Decimal("-1.000")]
    assert balance == Decimal("14.000")


def test_charged_to_annual_leave_with_nothing_held_falls_unpaid_and_charges_nothing(
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
    """The `annual_leave` election cannot pay leave that is not there: with no
    balance the day falls to unpaid exactly as an ordinary overdraw does
    (D-188) — unpaid, and the balance is not driven negative."""
    _elect(employee, "annual_leave")
    application, day, balance = _unauthorised_day(
        employee, annual_type, annual_unauthorised_type, owner, "0.500"
    )

    assert application.exceeds_balance is True
    assert day.is_paid is False
    assert day.deducted_from_balance is False
    assert application.unpaid_days == Decimal("1.000")
    assert _taken(application) == []
    assert balance == Decimal("0.500")


def test_the_election_is_frozen_on_the_application_when_it_is_submitted(
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
    """Changing the setting after submission must not rewrite an absence
    already recorded: the treatment lives on the application's day rows."""
    from employers.models import EmployerSetting

    cycle = _hold(employee, annual_type, "15.000", LeaveCycle.Unit.DAYS)
    application = submit_application(
        employee, leave_type=annual_unauthorised_type, start_date=MONDAY, end_date=MONDAY
    )
    _elect(employee, "annual_leave")
    approve(application, decided_by=owner)

    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
        assert EmployerSetting.objects.get(setting_key="UNAUTHORISED_ABSENCE_TREATMENT")
    assert application.unpaid_days == Decimal("1.000")
    assert balance == Decimal("15.000")


def test_an_unknown_treatment_is_refused_by_name_not_guessed(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    annual_unauthorised_type,
    schedule_5day,
    working_time_rules,
):
    """A stored value outside the choices must not silently fall back to the
    default — that would be the default_sort() shape again."""
    from employers.onboarding import SettingValueError

    _hold(employee, annual_type, "15.000", LeaveCycle.Unit.DAYS)
    _elect(employee, "sometimes")

    with pytest.raises(SettingValueError) as raised:
        submit_application(
            employee, leave_type=annual_unauthorised_type, start_date=MONDAY, end_date=MONDAY
        )
    message = str(raised.value)
    assert "UNAUTHORISED_ABSENCE_TREATMENT" in message, message
    assert "'sometimes'" in message, message
    assert "annual_leave, unpaid" in message, message


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
    """D-196, Kobus's decision: sick leave withheld for want of a certificate
    beyond the BCEA s23(1) threshold is UNPAID and NOT charged — the s22
    entitlement is untouched. Was D-193's second defect: balance 25."""
    cycle = _hold(employee, sick_type, "30.000", LeaveCycle.Unit.DAYS)

    application = submit_application(
        employee, leave_type=sick_type, start_date=MONDAY, end_date=FRIDAY
    )
    approve(application, decided_by=owner)

    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
    assert application.unpaid_days == Decimal("5.000")
    assert balance == Decimal("30.000"), f"charged AND unpaid: balance {balance}"
    assert _taken(application) == [], "uncertified and unpaid: no TAKEN row"


def test_sick_leave_within_the_certificate_threshold_is_paid_and_charged(
    employee,
    engagement,
    leave_rules,
    sick_type,
    evidence_types,
    schedule_5day,
    owner,
    working_time_rules,
):
    """The negative half of D-196: two days needs no certificate (s23(1)), so they
    are ordinary sick leave — paid AND drawn from the balance."""
    cycle = _hold(employee, sick_type, "30.000", LeaveCycle.Unit.DAYS)

    application = submit_application(
        employee, leave_type=sick_type, start_date=MONDAY, end_date=datetime.date(2026, 3, 3)
    )
    approve(application, decided_by=owner)

    with tenant_context(employee.tenant_id):
        balance = recompute_cycle(cycle).balance_quantity
    assert application.unpaid_days == Decimal("0")
    assert [t.days for t in _taken(application)] == [Decimal("-2.000")]
    assert balance == Decimal("28.000")
