"""``public_holiday_observance`` — task 4, task 6.

THIS CHANGES CHUNK 2'S ANSWER, and that is the point: an employer that did
not observe a statutory public holiday works it as ordinary, and that day
IS deducted from leave — on the exact same calendar date a different
employer's employee still gets it off, unpaid-by-leave, for free.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context, tenant_context_of
from employees.engagements import engage
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from employers.models import Employer
from leave.applications import submit_application
from leave.cycles import ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveApplicationDay, LeaveCycle, LeaveTransaction, PublicHolidayObservance
from leave.tests.conftest import BORN, START, make_id

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType
MONDAY = datetime.date(2026, 3, 2)
HOLIDAY = datetime.date(2026, 3, 4)  # Wednesday, inside the fixture week
FRIDAY = datetime.date(2026, 3, 6)


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


@pytest.fixture
def other_employer(tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Other Household", sector=sector)


@pytest.fixture
def other_employee(tenant, other_employer, minimum_age):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=other_employer,
            first_name="Palesa",
            last_name="Dlamini",
            date_of_birth=BORN,
            mobile_number="+27820000099",
            email="palesa@example.com",
            id_number=make_id(sequence="7001"),
        )
    engage(person, start_date=START, job_title="Domestic worker")
    with tenant_context(tenant.pk):
        made = WorkSchedule.objects.create(
            tenant=tenant,
            employee=person,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=made,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
    return person


def test_an_employer_with_no_override_still_treats_the_calendar_holiday_as_not_working(
    employee, engagement, leave_rules, annual_type, schedule_5day, public_holiday_wednesday
):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert application.total_days == Decimal("4.000"), "Wednesday is not deducted."
    with tenant_context_of(employee):
        wednesday = LeaveApplicationDay.objects.get(
            leave_application=application, leave_date=HOLIDAY
        )
    assert wednesday.is_working_day is False
    assert wednesday.is_public_holiday is True


def test_an_employer_who_did_not_observe_it_works_it_as_ordinary_and_it_is_deducted(
    other_employer,
    other_employee,
    leave_rules,
    annual_type,
    public_holiday_wednesday,
    tenant,
):
    with tenant_context(tenant.pk):
        PublicHolidayObservance.objects.create(
            tenant=tenant,
            employer=other_employer,
            public_holiday=public_holiday_wednesday,
            observance_date=HOLIDAY,
            name="Worked by agreement",
            is_observed=False,
        )

    _grant_balance(other_employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        other_employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert application.total_days == Decimal("5.000"), (
        "Not observed: Wednesday is worked as ordinary, so all five days deduct."
    )
    with tenant_context_of(other_employee):
        wednesday = LeaveApplicationDay.objects.get(
            leave_application=application, leave_date=HOLIDAY
        )
    assert wednesday.is_working_day is True
    assert wednesday.is_public_holiday is False
    assert wednesday.deducted_from_balance is True


def test_both_directions_on_the_same_date_for_two_employers(
    employee,
    engagement,
    leave_rules,
    annual_type,
    schedule_5day,
    public_holiday_wednesday,
    other_employer,
    other_employee,
    tenant,
):
    """THE TEST TASK 4 ASKS FOR BY NAME: one calendar date, two employers,
    two different outcomes."""
    with tenant_context(tenant.pk):
        PublicHolidayObservance.objects.create(
            tenant=tenant,
            employer=other_employer,
            public_holiday=public_holiday_wednesday,
            observance_date=HOLIDAY,
            name="Worked by agreement",
            is_observed=False,
        )

    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))
    _grant_balance(other_employee, annual_type, quantity=Decimal("15.000"))

    observing = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )
    not_observing = submit_application(
        other_employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert observing.total_days == Decimal("4.000")
    assert not_observing.total_days == Decimal("5.000")


def test_an_employer_specific_day_with_no_statutory_holiday_is_also_honoured(
    employer, employee, engagement, leave_rules, annual_type, schedule_5day, tenant
):
    """``public_holiday`` is nullable — an employer can declare its own day
    off the statutory calendar knows nothing about, sheet 02's own reason
    for the column."""
    company_day = datetime.date(2026, 3, 5)  # an ordinary Thursday, no statutory holiday
    with tenant_context(tenant.pk):
        PublicHolidayObservance.objects.create(
            tenant=tenant,
            employer=employer,
            public_holiday=None,
            observance_date=company_day,
            name="Company anniversary",
            is_observed=True,
        )

    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert application.total_days == Decimal("4.000")
    with tenant_context_of(employee):
        thursday = LeaveApplicationDay.objects.get(
            leave_application=application, leave_date=company_day
        )
    assert thursday.is_working_day is False
