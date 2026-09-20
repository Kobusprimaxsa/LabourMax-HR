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
    run_monthly_accrual,
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


def test_s22_4_not_exercised_makes_the_full_entitlement_available_with_no_deduction(
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
    """SICK_FIRST_CYCLE_REDUCTION = FALSE: this employer has elected NOT to
    exercise BCEA s22(4). At six months the FULL s22(2) entitlement becomes
    available and the day already taken under s22(3) is NOT deducted — so
    across cycle one the employee may draw E + A, not E.

    The same ledger as the default-branch test above (2 accrued, 1 taken),
    so the two figures differ by exactly the one day s22(4) is about. The
    batch entry point is called with NOTHING pinned at the call site, the
    way a Celery task arrives. Asserts on the basis and the reason as well
    as the total, so a coincidentally-equal figure cannot pass it.
    """
    from dateutil.relativedelta import relativedelta

    from employers.models import EmployerSetting
    from leave.accrual import SICK_TRANSITION_UNREDUCED_BASIS

    with tenant_context_of(employee):
        EmployerSetting.objects.create(
            tenant=employer.tenant,
            employer=employer,
            setting_key="SICK_FIRST_CYCLE_REDUCTION",
            value_type=EmployerSetting.ValueType.BOOLEAN,
            value_boolean=False,
            set_by_employer=True,
        )
        cycle = ensure_cycles(employee, sick_type, horizon=START)[0]
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

    transition_date = cycle.cycle_start + relativedelta(months=6)
    run = run_monthly_accrual(employer, sick_type, transition_date)
    assert run.transactions_created == 1

    with tenant_context_of(employee):
        txn = LeaveTransaction.objects.get(leave_cycle=cycle, transaction_date=transition_date)
        recomputed = recompute_cycle(cycle)

    # E, from the rule set and the schedule — six weeks of a five-day week.
    entitlement = leave_rules.sick_leave_weeks_equivalent * Decimal("5")
    assert recomputed.entitlement_quantity == entitlement

    assert txn.calculation_basis == SICK_TRANSITION_UNREDUCED_BASIS, (
        f"the unreduced transition must say so in its basis, got {txn.calculation_basis!r}"
    )
    assert "s22(4) not exercised" in txn.reason, txn.reason
    assert "SICK_FIRST_CYCLE_REDUCTION" in txn.reason, txn.reason
    assert txn.days == entitlement - Decimal("1.000"), (
        f"top-up must bring a balance of 1 to the full {entitlement}, got {txn.days}"
    )
    assert recomputed.balance_quantity == entitlement, (
        f"election OFF: the full entitlement ({entitlement}) must be available with "
        f"NO deduction for the one day taken under the ratio — got "
        f"{recomputed.balance_quantity}."
    )

    # Idempotent: the unreduced transition is still THE transition.
    with tenant_context_of(employee):
        again = accrue_employee(employee, sick_type, as_at=transition_date)
    assert again is None


def test_the_s22_4_election_is_read_with_the_employers_tenant_pinned_by_the_resolver(
    employer, sector
):
    """The default_sort() bug, guarded for this setting: read with NOTHING
    pinned, a resolver that does not pin the tenant finds no row, falls back
    to the registry default (TRUE) and is silently wrong for exactly the
    employer who elected FALSE."""
    from employers.models import EmployerSetting
    from employers.onboarding import setting_value

    with tenant_context_of(employer):
        EmployerSetting.objects.create(
            tenant=employer.tenant,
            employer=employer,
            setting_key="SICK_FIRST_CYCLE_REDUCTION",
            value_type=EmployerSetting.ValueType.BOOLEAN,
            value_boolean=False,
            set_by_employer=True,
        )

    assert setting_value(employer, "SICK_FIRST_CYCLE_REDUCTION") is False


@pytest.mark.parametrize(
    ("reduce_by_taken", "accrued", "taken", "expected_top_up", "expected_balance"),
    [
        # E = 30 throughout. Balance going in is always accrued - taken.
        (True, "2", "1", "28", "29"),  # s22(4) exercised: E - T
        (False, "2", "1", "29", "30"),  # not exercised: E, T not deducted
        (True, "4", "4", "26", "26"),  # took everything available: E - T
        (False, "4", "4", "30", "30"),
        (True, "3", "0", "27", "30"),  # nothing drawn: the election is inert
        (False, "3", "0", "27", "30"),
    ],
)
def test_sick_first_cycle_top_up_is_pure_arithmetic_over_one_entitlement(
    reduce_by_taken, accrued, taken, expected_top_up, expected_balance
):
    """No database: the election arrives as a resolved boolean. Where nothing
    was drawn the two elections must agree — s22(4) only has anything to
    act on when sick leave was actually taken under s22(3)."""
    from leave.accrual import sick_first_cycle_top_up

    top_up = sick_first_cycle_top_up(
        entitlement=Decimal("30"),
        accrued=Decimal(accrued),
        taken=Decimal(taken),
        reduce_by_taken=reduce_by_taken,
    )
    assert top_up == Decimal(expected_top_up)
    assert Decimal(accrued) - Decimal(taken) + top_up == Decimal(expected_balance)


def test_s22_4_not_exercised_does_not_add_back_a_sick_day_that_was_cancelled(
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
    """A reversed TAKEN row was never drawn. Adding it back under the
    unreduced election would hand the employee a day they already got back
    when the application was cancelled — E + 1 rather than E."""
    from dateutil.relativedelta import relativedelta

    from employers.models import EmployerSetting

    with tenant_context_of(employee):
        EmployerSetting.objects.create(
            tenant=employer.tenant,
            employer=employer,
            setting_key="SICK_FIRST_CYCLE_REDUCTION",
            value_type=EmployerSetting.ValueType.BOOLEAN,
            value_boolean=False,
            set_by_employer=True,
        )
        cycle = ensure_cycles(employee, sick_type, horizon=START)[0]
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
        taken = post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=sick_type,
            transaction_type=TransactionType.TAKEN,
            quantity=Decimal("-1.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 4, 15),
            calculation_basis="manual",
        )
        reverse_transaction(taken, reason="application cancelled")

        transition_date = cycle.cycle_start + relativedelta(months=6)
        accrue_employee(employee, sick_type, as_at=transition_date)
        recomputed = recompute_cycle(cycle)

    assert recomputed.balance_quantity == recomputed.entitlement_quantity


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


def test_family_responsibility_is_granted_once_per_cycle_as_one_transaction(
    employer,
    employee,
    engagement,
    minimum_age,
    leave_rules,
    family_type,
    working_time_rules,
    schedule_5day,
):
    # Granted once the employee is eligible under s27(1) (D-189) — longer than
    # the rule set's four months — and dated that first eligible day, which in
    # cycle one is later than cycle start.
    from leave.eligibility import family_responsibility_eligibility

    with tenant_context_of(employee):
        eligible_from = family_responsibility_eligibility(employee, START).eligible_from
        txn = accrue_employee(employee, family_type, as_at=eligible_from)

    assert txn is not None
    assert txn.days == Decimal("3.000"), "leave_rule_set's own family_responsibility_days."
    assert txn.transaction_date == eligible_from
    assert txn.calculation_basis == FAMILY_RESPONSIBILITY_BASIS
    assert txn.transaction_type == TransactionType.ACCRUAL

    # Idempotent: a second call the same cycle grants nothing further.
    with tenant_context_of(employee):
        again = accrue_employee(
            employee, family_type, as_at=eligible_from + datetime.timedelta(days=60)
        )
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
        # After s27(1)'s four months (D-189) — before it nothing is granted.
        assert accrue_employee(employee, family_type, as_at=datetime.date(2026, 7, 31))
        post_transaction(
            employee=employee,
            leave_cycle=cycle_one,
            leave_type=family_type,
            transaction_type=TransactionType.TAKEN,
            quantity=Decimal("-2.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 8, 10),
            calculation_basis="manual",
        )
        assert recompute_cycle(cycle_one).balance_quantity == Decimal("1.000")

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
    # D-195: the balance is charged only when the employer has elected to treat
    # an unauthorised absence as annual leave — and then the day is PAID too.
    # The default, `unpaid`, charges nothing (test_unpaid_days_are_not_charged).
    from employers.models import EmployerSetting

    with tenant_context_of(employee):
        EmployerSetting.objects.create(
            tenant=employee.tenant,
            employer=employer,
            setting_key="UNAUTHORISED_ABSENCE_TREATMENT",
            value_type=EmployerSetting.ValueType.TEXT,
            value_text="annual_leave",
            set_by_employer=True,
        )
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
    with tenant_context_of(employee):
        assert application.days.get().is_paid is True, "charged to annual leave, so paid"


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


# ------------------------------------------------- units: SICK and FAMILY (D-190)


@pytest.mark.parametrize("code", ["SICK", "FAMILY_RESPONSIBILITY"])
def test_a_per_hours_worked_agreement_never_makes_a_day_entitlement_an_hours_cycle(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    sick_type,
    family_type,
    sick_first_period,
    working_time_rules,
    schedule_5day,
    code,
):
    """Found by the property generator (D-190). BCEA s20(2)'s per-hours-worked
    method is an ANNUAL leave agreement. s22(2) and s27(2) state their
    entitlements in days, and the engine computes days for them — so an
    entitlement row carrying PER_HOURS_WORKED for either used to produce an
    HOURS cycle, and the engine then wrote day figures into the hours column:
    the ledger still summed, so nothing reconciling it could see."""
    from django.db import connection

    from employees.models import EmployeeLeaveEntitlement
    from employees.statutory_methods import TRIGGER_NAME

    leave_type = sick_type if code == "SICK" else family_type
    # D-192 now refuses this row at capture. The engine's ignore is the second
    # layer, for a row that PREDATES that refusal — simulated by writing it with
    # the trigger disabled.
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE employee_leave_entitlement DISABLE TRIGGER {TRIGGER_NAME}")
    with tenant_context_of(employee):
        EmployeeLeaveEntitlement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            leave_type=leave_type,
            accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
            effective_from=START,
        )
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE employee_leave_entitlement ENABLE TRIGGER {TRIGGER_NAME}")
    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, leave_type, horizon=START)[0]

    assert cycle.unit == LeaveCycle.Unit.DAYS, f"{code} is an entitlement in days, got {cycle.unit}"


def test_a_reversed_accrual_is_not_deducted_from_the_six_month_top_up(
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
    """Found by the property generator (D-190). A ratio accrual posted in
    error and reversed was never accrued. The transition summed GROSS
    accruals, so the reversed 2 days were still subtracted from the top-up:
    one real day accrued, nothing drawn, and the employee reached six months
    holding E - 2. Default election — the D-181 test above is untouched."""
    from dateutil.relativedelta import relativedelta

    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, sick_type, horizon=START)[0]
        mistaken = post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=sick_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("2.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 3, 28),
            calculation_basis="per_26_days_first_6m",
        )
        reverse_transaction(mistaken, reason="accrued against the wrong month")
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=sick_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal("1.000"),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=datetime.date(2026, 4, 28),
            calculation_basis="per_26_days_first_6m",
        )
        txn = accrue_employee(
            employee, sick_type, as_at=cycle.cycle_start + relativedelta(months=6)
        )
        recomputed = recompute_cycle(cycle)

    assert txn.calculation_basis == SICK_TRANSITION_BASIS
    assert txn.days == recomputed.entitlement_quantity - Decimal("1.000"), txn.days
    assert recomputed.balance_quantity == recomputed.entitlement_quantity, (
        f"nothing was drawn, so the whole entitlement ({recomputed.entitlement_quantity}) "
        f"must be available — got {recomputed.balance_quantity}"
    )


# ------------- s22(3) is an AVAILABILITY restriction, not a second accrual


def test_s22_3_restricts_availability_within_one_entitlement_not_a_second_accrual(
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
    """THE MODEL SHAPE, pinned (D-181, confirmed against the Act 20 Sep 2026).

    s22(3)'s one-day-per-26 can be modelled two ways, and **they agree in the
    ordinary case and diverge the moment anything else changes**, which is the
    worst kind of agreement:

    * an AVAILABILITY restriction inside the ONE s22(2) entitlement — the ratio
      amount A is part of E, and the transition tops up by E − A so the cycle
      has granted E in total; or
    * a SECOND accrual with its own balance — A is additional to E, and the
      cycle would end up having granted A + E.

    This is the first. The decisive assertion is the SUM OF ACCRUALS over the
    cycle, not the balance: with A = 2 and T = 1 the balance is E − 1 under the
    first reading and E + 1 under the second, but it is the accrual total that
    says *why*. ``sick_first_cycle_top_up()`` returning ``entitlement - accrued``
    is only coherent if the ratio accruals were already part of the entitlement.

    Also pinned: there is exactly ONE sick cycle across the whole period, and
    its ``entitlement_quantity`` never moves. A second entitlement would have to
    live somewhere, and this is the assertion that would find it.
    """
    from dateutil.relativedelta import relativedelta

    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, sick_type, horizon=START)[0]
        entitlement_at_start = cycle.entitlement_quantity

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

    transition_date = cycle.cycle_start + relativedelta(months=6)
    with tenant_context_of(employee):
        accrue_employee(employee, sick_type, as_at=transition_date)

        # ONE cycle. s22(1)(a) runs it from commencement of employment and
        # leave_type.cycle_months is 36, so the first six months are INSIDE
        # cycle 1 rather than a cycle of their own.
        assert LeaveCycle.objects.filter(employee=employee, leave_type=sick_type).count() == 1

        granted = sum(
            (
                txn.days
                for txn in LeaveTransaction.objects.filter(
                    leave_cycle=cycle, transaction_type=TransactionType.ACCRUAL
                )
            ),
            Decimal("0"),
        )

    recomputed = recompute_cycle(cycle)

    assert recomputed.entitlement_quantity == entitlement_at_start, (
        "the one entitlement does not move when the ratio phase ends"
    )
    assert granted == entitlement_at_start, (
        f"the cycle must have granted EXACTLY the one s22(2) entitlement "
        f"({entitlement_at_start}), not the ratio amount on top of it "
        f"({entitlement_at_start + Decimal('2.000')}). Granted {granted}: "
        f"s22(3) has become a second accrual with its own balance."
    )
    assert recomputed.balance_quantity == entitlement_at_start - Decimal("1.000")
