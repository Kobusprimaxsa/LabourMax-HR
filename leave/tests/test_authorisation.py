"""Approving, declining and cancelling — task 3, task 4, task 6.

Self-approval is blocked and escalates to the owner; only the sole owner,
with no other approver, may self-approve, and only with a reason on the
record. Approving refuses a day already captured as worked, naming the
date, and a locked day, naming the run. Cancelling reverses the ledger
exactly and restores the balance.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.capture import capture
from attendance.models import AttendanceDay
from calculators.attendance import DayType
from core.managers import tenant_context, tenant_context_of
from core.models import AppUser, TenantMembership
from leave.applications import submit_application
from leave.authorisation import AuthorisationRefusedError, approve, cancel, decline
from leave.balances import recompute_cycle
from leave.cycles import current_cycle, ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveApplication, LeaveCycle, LeaveTransaction

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType

MONDAY = datetime.date(2026, 3, 2)
TUESDAY = datetime.date(2026, 3, 3)


def _grant_balance(employee, leave_type, *, quantity: Decimal, on_date=MONDAY):
    cycle = ensure_cycles(employee, leave_type, horizon=on_date)[0]
    post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=leave_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=quantity,
        unit=LeaveCycle.Unit.DAYS,
        transaction_date=on_date,
        calculation_basis="manual",
    )
    return cycle


@pytest.fixture
def owner_user(db):
    return AppUser.objects.create_user(email="owner@example.com", password="x" * 16)


@pytest.fixture
def owner_membership(tenant, owner_user):
    with tenant_context(tenant.pk):
        return TenantMembership.objects.create(
            tenant=tenant, user=owner_user, role=TenantMembership.Role.OWNER
        )


@pytest.fixture
def admin_user(db):
    return AppUser.objects.create_user(email="admin@example.com", password="x" * 16)


@pytest.fixture
def admin_membership(tenant, admin_user):
    with tenant_context(tenant.pk):
        return TenantMembership.objects.create(
            tenant=tenant, user=admin_user, role=TenantMembership.Role.ADMIN
        )


@pytest.fixture
def application(employee, engagement, leave_rules, annual_type, schedule_5day, working_time_rules):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))
    return submit_application(employee, leave_type=annual_type, start_date=MONDAY, end_date=MONDAY)


# --------------------------------------------------------------- approval


def test_approve_writes_the_ledger_and_the_attendance_day(
    application, owner_user, owner_membership
):
    approved = approve(application, decided_by=owner_user)

    assert approved.status == LeaveApplication.Status.APPROVED
    with tenant_context_of(application):
        day = AttendanceDay.objects.get(employee=application.employee, work_date=MONDAY)
        assert day.day_type == AttendanceDay.DayType.LEAVE
        assert day.leave_application_id == application.pk

        txn = LeaveTransaction.objects.get(
            leave_application=application, transaction_type=TransactionType.TAKEN
        )
    assert txn.days == Decimal("-1.000")


def test_approving_over_a_day_already_worked_is_refused_naming_the_date(
    application, owner_user, owner_membership
):
    with tenant_context_of(application):
        capture(
            employee=application.employee,
            work_date=MONDAY,
            day_type=DayType.ORDINARY,
            time_in=datetime.time(8, 0),
            time_out=datetime.time(17, 0),
        )

    with pytest.raises(AuthorisationRefusedError) as raised:
        approve(application, decided_by=owner_user)

    assert "2 March 2026" in str(raised.value) or "02 March 2026" in str(raised.value)
    assert "already captured as worked" in str(raised.value)


def test_approving_over_a_locked_day_is_refused_naming_the_run(
    application, owner_user, owner_membership
):
    with tenant_context_of(application):
        AttendanceDay.objects.create(
            tenant=application.tenant,
            employee=application.employee,
            work_date=MONDAY,
            day_type=AttendanceDay.DayType.NO_WORK_AVAILABLE,
            status=AttendanceDay.Status.LOCKED,
            locked_by_payroll_run_id_ref=99,
        )

    with pytest.raises(AuthorisationRefusedError) as raised:
        approve(application, decided_by=owner_user)

    assert "99" in str(raised.value)
    assert "locked" in str(raised.value).lower()


def test_declining_writes_nothing_to_the_ledger_or_attendance(
    application, owner_user, owner_membership
):
    declined = decline(application, decided_by=owner_user, decision_comment="not approved")

    assert declined.status == LeaveApplication.Status.DECLINED
    with tenant_context_of(application):
        assert not AttendanceDay.objects.filter(employee=application.employee).exists()
        assert not LeaveTransaction.objects.filter(leave_application=application).exists()


def test_approving_a_non_submitted_application_is_refused(
    application, owner_user, owner_membership
):
    approve(application, decided_by=owner_user)

    with pytest.raises(AuthorisationRefusedError):
        approve(application, decided_by=owner_user)


# ---------------------------------------------------------- self-approval


def test_self_approval_by_a_non_owner_is_refused(application, admin_user, tenant):
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(
            tenant=tenant,
            user=admin_user,
            role=TenantMembership.Role.ADMIN,
            employee_id_ref=application.employee.pk,
        )

    with pytest.raises(AuthorisationRefusedError) as raised:
        approve(application, decided_by=admin_user)

    assert "self-approval is blocked" in str(raised.value).lower()


def test_self_approval_by_the_sole_owner_requires_a_reason(application, owner_user, tenant):
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(
            tenant=tenant,
            user=owner_user,
            role=TenantMembership.Role.OWNER,
            employee_id_ref=application.employee.pk,
        )

    with pytest.raises(AuthorisationRefusedError) as raised:
        approve(application, decided_by=owner_user)

    assert "reason" in str(raised.value).lower()


def test_self_approval_by_the_sole_owner_is_permitted_with_a_reason(
    application, owner_user, tenant
):
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(
            tenant=tenant,
            user=owner_user,
            role=TenantMembership.Role.OWNER,
            employee_id_ref=application.employee.pk,
        )

    approved = approve(
        application,
        decided_by=owner_user,
        self_approval_reason="No other admin exists for this household yet.",
    )

    assert approved.self_approved is True
    assert approved.self_approval_reason == "No other admin exists for this household yet."


def test_self_approval_by_the_owner_is_refused_when_another_approver_exists(
    application, owner_user, admin_user, tenant
):
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(
            tenant=tenant,
            user=owner_user,
            role=TenantMembership.Role.OWNER,
            employee_id_ref=application.employee.pk,
        )
        TenantMembership.objects.create(
            tenant=tenant, user=admin_user, role=TenantMembership.Role.ADMIN
        )

    with pytest.raises(AuthorisationRefusedError) as raised:
        approve(application, decided_by=owner_user, self_approval_reason="I'll do it myself")

    assert "another approver is available" in str(raised.value).lower()


# --------------------------------------------------------------- cancel


def test_cancelling_reverses_the_ledger_and_the_balance_returns(
    application, owner_user, owner_membership
):
    cycle = current_cycle(application.employee, application.leave_type, MONDAY)
    before = recompute_cycle(cycle).balance_quantity

    approve(application, decided_by=owner_user)
    after_approval = recompute_cycle(cycle).balance_quantity
    assert after_approval == before - Decimal("1.000")

    cancelled = cancel(application, cancelled_reason="Employee withdrew the request")

    assert cancelled.status == LeaveApplication.Status.CANCELLED
    after_cancel = recompute_cycle(cycle).balance_quantity
    assert after_cancel == before, "The balance returns to its prior figure, exactly."

    with tenant_context_of(application):
        reversal = LeaveTransaction.objects.get(transaction_type=TransactionType.REVERSAL)
        assert reversal.days == Decimal("1.000")
        assert not AttendanceDay.objects.filter(leave_application=application).exists()


def test_cancelling_after_the_leave_was_taken_still_reverses(
    application, owner_user, owner_membership
):
    approve(application, decided_by=owner_user)

    cancelled = cancel(
        application, cancelled_reason="Discovered after the fact — employee never took the day"
    )

    assert cancelled.status == LeaveApplication.Status.CANCELLED
    with tenant_context_of(application):
        assert LeaveTransaction.objects.filter(
            leave_application=application, transaction_type=TransactionType.REVERSAL
        ).exists()


def test_cancelling_without_a_reason_is_refused(application, owner_user, owner_membership):
    approve(application, decided_by=owner_user)

    with pytest.raises(AuthorisationRefusedError):
        cancel(application, cancelled_reason="")


def test_cancelling_a_non_approved_application_is_refused(application):
    with pytest.raises(AuthorisationRefusedError):
        cancel(application, cancelled_reason="never approved")
