"""P6 chunk 4, task 6 — sick leave accrual, family responsibility, unauthorised
annual leave's parent balance, and the reversal case that started task 1.

Every guard here is proven against the case it exists for, per this
codebase's own house rule: the six-month transition asserts the TOTAL
balance (not the delta) because a delta assertion could pass even if the
transition doubled or stranded what was taken; the threshold test drives a
REAL accrued balance through the real engine rather than a hand-posted one,
so the two pieces of chunk 2 and chunk 4 are proven to compose.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context, tenant_context_of
from core.models import AppUser, TenantMembership
from leave.accrual import (
    FAMILY_RESPONSIBILITY_BASIS,
    SICK_TRANSITION_BASIS,
    SICK_UPFRONT_BASIS,
    accrue_employee,
)
from leave.applications import submit_application
from leave.authorisation import approve
from leave.balances import recompute_cycle
from leave.cycles import current_cycle, ensure_cycles
from leave.ledger import post_transaction, reverse_transaction
from leave.models import LeaveApplication, LeaveApplicationDay, LeaveCycle, LeaveTransaction
from leave.negative_balances import negative_balances

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType

START = datetime.date(2026, 3, 1)  # a Sunday; the first Monday is 2026-03-02
MONDAY = datetime.date(2026, 3, 2)


@pytest.fixture
def owner_user(db):
    return AppUser.objects.create_user(email="chunk4-owner@example.com", password="x" * 16)


@pytest.fixture
def owner_membership(tenant, owner_user):
    with tenant_context(tenant.pk):
        return TenantMembership.objects.create(
            tenant=tenant, user=owner_user, role=TenantMembership.Role.OWNER
        )


def _capture_worked_days(employee, dates):
    """Full ordinary days, worked in full — ``days_worked_equivalent`` == 1
    for each, matching the fixture 8-to-4 schedule exactly."""
    from attendance.capture import capture
    from calculators.attendance import DayType

    for a_date in dates:
        capture(
            employee,
            work_date=a_date,
            day_type=DayType.ORDINARY,
            time_in=datetime.time(8, 0),
            time_out=datetime.time(17, 0),
            unpaid_break_minutes=60,
        )


# --------------------------------------------------------- SICK, first phase


def test_sick_leave_accrues_from_days_worked_not_calendar_days(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    sick_type,
    sick_first_period,
    working_time_rules,
    schedule_5day,
):
    """Month three of employment (May 2026): five weekdays actually worked
    accrue five days' worth of the ratio; the calendar month itself has far
    more days than that, which is exactly the point — a wrong
    implementation that used calendar days instead of attendance would
    accrue a different, larger figure."""
    as_at = datetime.date(2026, 5, 28)  # month 3; well inside the 6-month ratio phase
    _capture_worked_days(
        employee,
        [
            datetime.date(2026, 5, 4),
            datetime.date(2026, 5, 5),
            datetime.date(2026, 5, 6),
            datetime.date(2026, 5, 7),
            datetime.date(2026, 5, 8),
        ],
    )

    with tenant_context_of(employee):
        txn = accrue_employee(employee, sick_type, as_at=as_at)

    assert txn is not None
    assert txn.days == (Decimal("5") / Decimal("26")).quantize(Decimal("0.001")), (
        "5 days worked / the rule set's own 26-day ratio, not a calendar-day count."
    )
    assert txn.hours is None
    assert txn.calculation_basis == "per_26_days_first_6m"


def test_someone_who_worked_fewer_days_accrues_less_sick_leave(
    employer,
    tenant,
    sector,
    minimum_age,
    leave_rules,
    sick_type,
    sick_first_period,
    working_time_rules,
):
    """Two otherwise-identical employees, same month, different attendance —
    the one who worked less accrues strictly less."""
    from employees.engagements import engage
    from employees.identity import luhn_check_digit
    from employees.models import Employee, WorkSchedule, WorkScheduleDay

    def make_id(seq):
        body = f"900101{seq}08"
        return body + str(luhn_check_digit(body))

    def _make_employee(seq, email):
        with tenant_context(tenant.pk):
            person = Employee.objects.create(
                tenant=tenant,
                employer=employer,
                first_name="Sick",
                last_name=f"Test{seq}",
                date_of_birth=datetime.date(1990, 1, 1),
                mobile_number=f"+2782{seq}",
                email=email,
                id_number=make_id(seq),
            )
        engage(person, start_date=START, job_title="Domestic worker")
        with tenant_context(tenant.pk):
            schedule = WorkSchedule.objects.create(
                tenant=tenant,
                employee=person,
                days_per_week=Decimal("5"),
                ordinary_hours_per_week=Decimal("40"),
                effective_from=START,
            )
            for cycle_day in range(7):
                WorkScheduleDay.objects.create(
                    tenant=tenant,
                    work_schedule=schedule,
                    cycle_day=cycle_day,
                    is_working_day=cycle_day < 5,
                    ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
                )
        return person

    full_attendance = _make_employee("7001", "full@example.com")
    partial_attendance = _make_employee("7002", "partial@example.com")

    as_at = datetime.date(2026, 5, 28)
    _capture_worked_days(
        full_attendance,
        [
            datetime.date(2026, 5, 4),
            datetime.date(2026, 5, 5),
            datetime.date(2026, 5, 6),
            datetime.date(2026, 5, 7),
            datetime.date(2026, 5, 8),
        ],
    )
    _capture_worked_days(partial_attendance, [datetime.date(2026, 5, 4), datetime.date(2026, 5, 5)])

    with tenant_context_of(full_attendance):
        full_txn = accrue_employee(full_attendance, sick_type, as_at=as_at)
    with tenant_context_of(partial_attendance):
        partial_txn = accrue_employee(partial_attendance, sick_type, as_at=as_at)

    assert full_txn is not None and partial_txn is not None
    assert partial_txn.days < full_txn.days, (
        "Fewer days actually worked must accrue strictly less sick leave."
    )


# ------------------------------------------------------- SICK, the transition


def test_sick_leave_transition_at_six_months_grants_the_full_entitlement_less_taken(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    sick_type,
    sick_first_period,
    working_time_rules,
    schedule_5day,
):
    """Some sick leave is taken during the ratio phase; at the six-month
    mark the cycle must hold EXACTLY the full six-week-equivalent minus
    what was taken — asserted as the TOTAL, not the delta the top-up
    transaction itself carries, because a total assertion is the only one
    that would catch either wrong reading task 2 named (doubling the
    entitlement, or stranding what was used early)."""
    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, sick_type, horizon=START)[0]

    # Ratio-phase accrual for month 1, then two days taken against it.
    with tenant_context_of(employee):
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=sick_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("2.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 3, 28),
            calculation_basis="per_26_days_first_6m",
        )
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=sick_type,
            transaction_type=TransactionType.TAKEN,
            quantity=Decimal("-1.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 4, 15),
            calculation_basis="manual",
        )

    from dateutil.relativedelta import relativedelta

    transition_date = cycle.cycle_start + relativedelta(months=6)

    with tenant_context_of(employee):
        txn = accrue_employee(employee, sick_type, as_at=transition_date)

    assert txn is not None
    assert txn.calculation_basis == SICK_TRANSITION_BASIS

    recomputed = recompute_cycle(cycle)
    full_entitlement = recomputed.entitlement_quantity  # six-week-equivalent, 5-day schedule

    assert recomputed.balance_quantity == full_entitlement - Decimal("1.000"), (
        f"the TOTAL after transition must be the full entitlement "
        f"({full_entitlement}) less the one day taken, not the delta the "
        f"top-up transaction itself carries."
    )

    # A second call at (or after) the transition date must not top up again.
    with tenant_context_of(employee):
        again = accrue_employee(employee, sick_type, as_at=transition_date)
    assert again is None


def test_sick_cycle_two_is_granted_upfront_with_no_ratio_phase(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    sick_type,
    sick_first_period,
    working_time_rules,
    schedule_5day,
):
    from dateutil.relativedelta import relativedelta

    cycle_two_start = START + relativedelta(months=36)
    with tenant_context_of(employee):
        ensure_cycles(employee, sick_type, horizon=cycle_two_start)
        cycle_two = current_cycle(employee, sick_type, cycle_two_start)
        assert cycle_two.cycle_number == 2

        txn = accrue_employee(employee, sick_type, as_at=cycle_two_start)

    assert txn is not None
    assert txn.calculation_basis == SICK_UPFRONT_BASIS
    recomputed = recompute_cycle(cycle_two)
    assert recomputed.balance_quantity == recomputed.entitlement_quantity


# --------------------------------------------------- sick pay threshold, real


def test_sick_leave_beyond_the_threshold_is_taken_and_unpaid_not_refused_with_a_real_balance(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    sick_type,
    sick_first_period,
    evidence_types,
    working_time_rules,
    schedule_5day,
    owner_user,
    owner_membership,
):
    """Chunk 2's rule (a sick application beyond the certificate threshold
    with no evidence is TAKEN and UNPAID, never refused) still holds now
    that the balance behind it comes from the real accrual engine rather
    than a hand-posted transaction."""
    _capture_worked_days(
        employee,
        [datetime.date(2026, 3, i) for i in (2, 3, 4, 5, 6)],
    )
    with tenant_context_of(employee):
        accrue_employee(employee, sick_type, as_at=datetime.date(2026, 3, 28))

    application = submit_application(
        employee,
        leave_type=sick_type,
        start_date=MONDAY,
        end_date=datetime.date(2026, 3, 6),  # 5 working days, no evidence
    )

    assert application.pk is not None, "The leave exists — it was never refused."
    assert application.status == LeaveApplication.Status.SUBMITTED
    with tenant_context_of(employee):
        paid_flags = set(
            LeaveApplicationDay.objects.filter(
                leave_application=application, is_working_day=True
            ).values_list("is_paid", flat=True)
        )
    assert paid_flags == {False}, "Beyond the threshold with no note: unpaid, not refused."


# --------------------------------------------------------- FAMILY_RESPONSIBILITY


def test_family_responsibility_is_granted_at_cycle_start_as_one_transaction(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    family_type,
    working_time_rules,
    schedule_5day,
):
    with tenant_context_of(employee):
        txn = accrue_employee(employee, family_type, as_at=START)

    assert txn is not None
    assert txn.days == Decimal("3.000"), "leave_rule_set's own family_responsibility_days."
    assert txn.transaction_date == START
    assert txn.calculation_basis == FAMILY_RESPONSIBILITY_BASIS
    assert txn.transaction_type == TransactionType.ACCRUAL

    # Idempotent: a second call the same cycle grants nothing further.
    with tenant_context_of(employee):
        again = accrue_employee(employee, family_type, as_at=START + datetime.timedelta(days=60))
    assert again is None

    with tenant_context_of(employee):
        count = LeaveTransaction.objects.filter(
            leave_type=family_type, transaction_type=TransactionType.ACCRUAL
        ).count()
    assert count == 1, "One grant per cycle — never a monthly accrual."


def test_family_responsibility_does_not_carry_over_between_cycles(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    family_type,
    working_time_rules,
    schedule_5day,
):
    """Cycle 1's grant is spent down to a fraction of itself; cycle 2 still
    gets exactly the rule set's own figure, with no addition for what was
    unused and no reduction for what was overspent — each cycle's grant
    stands alone."""
    from dateutil.relativedelta import relativedelta

    with tenant_context_of(employee):
        cycle_one = ensure_cycles(employee, family_type, horizon=START)[0]
        accrue_employee(employee, family_type, as_at=START)
        post_transaction(
            employee=employee,
            leave_cycle=cycle_one,
            leave_type=family_type,
            transaction_type=TransactionType.TAKEN,
            quantity=Decimal("-2.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 3, 10),
            calculation_basis="manual",
        )

    cycle_two_start = cycle_one.cycle_start + relativedelta(months=family_type.cycle_months)
    with tenant_context_of(employee):
        ensure_cycles(employee, family_type, horizon=cycle_two_start)
        txn_two = accrue_employee(employee, family_type, as_at=cycle_two_start)
        cycle_two = current_cycle(employee, family_type, cycle_two_start)

    assert txn_two is not None
    assert txn_two.days == Decimal("3.000"), (
        "Cycle 2's own grant, unrelated to cycle 1's leftover 1.000 balance."
    )
    recomputed = recompute_cycle(cycle_two)
    assert recomputed.balance_quantity == Decimal("3.000")


def test_family_responsibility_is_never_paid_out_on_termination(family_type):
    assert family_type.payable_on_termination is False


# --------------------------------------------------------- ANNUAL_UNAUTHORISED


def test_unauthorised_annual_leave_reduces_the_annual_balance(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    annual_unauthorised_type,
    working_time_rules,
    schedule_5day,
    owner_user,
    owner_membership,
):
    with tenant_context_of(employee):
        annual_cycle = ensure_cycles(employee, annual_type, horizon=MONDAY)[0]
        post_transaction(
            employee=employee,
            leave_cycle=annual_cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("15.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=START,
            calculation_basis="manual",
        )
        before = recompute_cycle(annual_cycle).balance_quantity

    application = submit_application(
        employee, leave_type=annual_unauthorised_type, start_date=MONDAY, end_date=MONDAY
    )
    approve(application, decided_by=owner_user)

    with tenant_context_of(employee):
        after = recompute_cycle(annual_cycle).balance_quantity
        unauthorised_cycle = current_cycle(employee, annual_unauthorised_type, MONDAY)

    assert unauthorised_cycle.pk == annual_cycle.pk, (
        "ANNUAL_UNAUTHORISED resolves to ANNUAL's own cycle — it has none of its own."
    )
    assert after == before - Decimal("1.000"), (
        "Taking unauthorised leave must reduce the ANNUAL balance — that is the "
        "entire point of recording it, and it failed silently before task 4."
    )
    with tenant_context_of(employee):
        taken_txn = LeaveTransaction.objects.get(
            leave_application=application, transaction_type=TransactionType.TAKEN
        )
    assert taken_txn.leave_type_id == annual_unauthorised_type.pk, (
        "The ledger row still carries its own label — ANNUAL_UNAUTHORISED — "
        "even though it posts against ANNUAL's cycle."
    )
    assert taken_txn.leave_cycle_id == annual_cycle.pk


# --------------------------------------------------------- negative balances


def test_negative_balance_query_reports_the_reversal_case_with_its_transactions(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    working_time_rules,
    schedule_5day,
):
    """The exact D-176 sequence: accrue, spend against it, reverse the
    accrual — the balance goes negative, honestly, and this query is what
    tells the employer rather than the system quietly holding it."""
    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, annual_type, horizon=START)[0]
        accrual = post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("2.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=START,
            calculation_basis="manual",
        )
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.TAKEN,
            quantity=Decimal("-2.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=START,
            calculation_basis="manual",
        )
        reverse_transaction(accrual, reason="posted in error")
        recompute_cycle(cycle)

    reported = negative_balances(employer)

    assert len(reported) == 1
    finding = reported[0]
    assert finding.cycle.pk == cycle.pk
    assert finding.employee_id == employee.pk
    assert finding.leave_type_code == annual_type.code
    assert finding.balance == Decimal("-2.000")
    assert finding.unit == LeaveCycle.Unit.DAYS
    assert len(finding.causing_transactions) == 3, "the accrual, the taken row, and its reversal."


def test_negative_balance_query_reports_nothing_for_a_healthy_balance(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    working_time_rules,
    schedule_5day,
):
    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, annual_type, horizon=START)[0]
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=annual_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("5.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=START,
            calculation_basis="manual",
        )

    assert negative_balances(employer) == []
