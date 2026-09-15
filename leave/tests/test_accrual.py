"""The monthly accrual engine (task 4) and its guards (task 6).

Includes THE NEGATIVE TEST THAT MATTERS —
``test_no_forfeiture_transaction_is_ever_written_automatically`` — named so
nobody deletes it by accident. It is the test that stops automatic
forfeiture creeping back into a job, a task or an engine path: decision D
says the employer captures forfeiture by hand, and this is what proves the
engine never does it for them.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from django.db.models import Sum

from core.managers import tenant_context, tenant_context_of
from employees.engagements import engage
from employees.models import Employee, EmployeeLeaveEntitlement
from leave.accrual import accrue_employee, run_monthly_accrual
from leave.balances import recompute_cycle
from leave.cycles import current_cycle
from leave.models import LeaveAccrualRun, LeaveCycle, LeaveTransaction
from leave.tests.conftest import BORN, make_id

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType
AccrualMethod = EmployeeLeaveEntitlement.AccrualMethod

START = datetime.date(2026, 3, 1)


def month_end_dates(start: datetime.date, count: int) -> list[datetime.date]:
    """``count`` dates, one per calendar month, on the 28th so every month
    (including February) has one — the boundary between cycle 1 and cycle 2
    is exercised by the CYCLE's own dates, not by a fragile day-of-month."""
    return [start.replace(day=28) + relativedelta(months=i) for i in range(count)]


# ------------------------------------------------------------- monthly, exact


def test_twelve_months_of_accrual_sums_exactly_to_the_ledger(
    employer, employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    for as_at in month_end_dates(START, 12):
        run_monthly_accrual(employer, annual_type, as_at)

    with tenant_context_of(employee):
        cycle = LeaveCycle.objects.get(employee=employee, leave_type=annual_type, cycle_number=1)
        ledger_total = LeaveTransaction.objects.filter(
            leave_cycle=cycle, transaction_type=TransactionType.ACCRUAL
        ).aggregate(total=Sum("days"))["total"]

    recomputed = recompute_cycle(cycle)

    assert ledger_total == Decimal("15.000"), "12 x 1.25/month, one row per month."
    assert recomputed.balance_quantity == ledger_total, "No drift between the cache and the ledger."


def test_run_monthly_accrual_is_idempotent(
    employer, employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    as_at = datetime.date(2026, 3, 28)

    first_run = run_monthly_accrual(employer, annual_type, as_at)
    with tenant_context_of(employee):
        count_after_first = LeaveTransaction.objects.filter(
            transaction_type=TransactionType.ACCRUAL
        ).count()

    second_run = run_monthly_accrual(employer, annual_type, as_at)
    with tenant_context_of(employee):
        count_after_second = LeaveTransaction.objects.filter(
            transaction_type=TransactionType.ACCRUAL
        ).count()

    assert second_run.pk == first_run.pk, "The same COMPLETED run, returned unchanged."
    assert count_after_second == count_after_first == 1
    assert second_run.transactions_created == first_run.transactions_created


def test_a_second_run_writes_nothing_even_via_a_fresh_call(
    employer, employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    """The UNIQUE constraint is the guarantee; the COMPLETED-run check is only
    what stops the ordinary case from ever reaching it. Assert on the row
    count directly rather than trusting the returned object alone."""
    as_at = datetime.date(2026, 3, 28)
    run_monthly_accrual(employer, annual_type, as_at)
    run_monthly_accrual(employer, annual_type, as_at)

    with tenant_context(employer.tenant_id):
        assert (
            LeaveAccrualRun.objects.filter(
                employer=employer, leave_type=annual_type, accrual_as_at=as_at
            ).count()
            == 1
        )


# --------------------------------------------------------- units never convert


@pytest.fixture
def hourly_employee(db, tenant, employer, minimum_age):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Sipho",
            last_name="Nkosi",
            date_of_birth=BORN,
            mobile_number="+27820000002",
            email="sipho@example.com",
            id_number=make_id(sequence="6001"),
        )
    engagement = engage(person, start_date=START, job_title="Domestic worker")
    return person, engagement


def test_hourly_employee_accrues_hours_never_days(
    hourly_employee, tenant, employer, leave_rules, annual_type
):
    person, engagement = hourly_employee

    with tenant_context(tenant.pk):
        EmployeeLeaveEntitlement.objects.create(
            tenant=tenant,
            employee=person,
            leave_type=annual_type,
            accrual_method=AccrualMethod.PER_HOURS_WORKED,
            effective_from=START,
        )
        from attendance.models import AttendanceDay

        AttendanceDay.objects.create(
            tenant=tenant,
            employee=person,
            work_date=datetime.date(2026, 3, 10),
            ordinary_hours=Decimal("17.000"),
        )

    with tenant_context_of(person):
        txn = accrue_employee(person, annual_type, as_at=datetime.date(2026, 3, 28))

    assert txn is not None
    assert txn.hours == Decimal("1.000"), "17 hours worked / 17-hour ratio = 1 hour accrued."
    assert txn.days is None


def test_monthly_employee_accrues_days_never_hours(
    employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    with tenant_context_of(employee):
        txn = accrue_employee(employee, annual_type, as_at=datetime.date(2026, 3, 28))

    assert txn is not None
    assert txn.days == Decimal("1.250")
    assert txn.hours is None


# ------------------------------------------------ THE NEGATIVE TEST THAT MATTERS


def test_no_forfeiture_transaction_is_ever_written_automatically(
    employer, employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    """Run the engine eighteen months forward — six months past cycle 1's own
    12-month end — and prove no forfeiture transaction exists anywhere, and
    cycle 1's own balance is exactly what it accrued, untouched.

    THIS IS THE TEST THAT STOPS AUTOMATIC FORFEITURE CREEPING BACK IN
    (decision D). No job, no scheduled task and no engine path may ever write
    a ``leave_transaction`` of type ``forfeiture`` — the employer captures it
    by hand, in a later chunk, and this test is the guard that catches a
    regression the moment anything in this engine tries to do it instead.
    """
    for as_at in month_end_dates(START, 18):
        run_monthly_accrual(employer, annual_type, as_at)

    with tenant_context_of(employee):
        forfeiture_count = LeaveTransaction.objects.filter(
            transaction_type=TransactionType.FORFEITURE
        ).count()
        cycle_one = LeaveCycle.objects.get(
            employee=employee, leave_type=annual_type, cycle_number=1
        )

    recomputed = recompute_cycle(cycle_one)

    assert forfeiture_count == 0, "No forfeiture transaction may exist. Ever. Not from this engine."
    assert recomputed.forfeited_quantity == Decimal("0.000")
    assert recomputed.balance_quantity == Decimal("15.000"), (
        "Cycle 1's full twelve months of accrual, carried in full — however many "
        "cycles have since passed, and with no cap applied (decision D)."
    )


def test_accrual_moves_to_the_new_cycle_once_the_old_one_ends(
    employer, employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    for as_at in month_end_dates(START, 18):
        run_monthly_accrual(employer, annual_type, as_at)

    second_cycle = current_cycle(employee, annual_type, datetime.date(2027, 6, 28))
    assert second_cycle is not None
    assert second_cycle.cycle_number == 2

    recomputed = recompute_cycle(second_cycle)
    assert recomputed.balance_quantity == Decimal("7.500"), "Six months into cycle 2 at 1.25/month."
