"""BCEA s27(1): family responsibility leave applies only to an employee who
has been employed by that employer for LONGER THAN four months AND who works
for that employer at least four days a week. Both limbs (D-189).

Every refusal is asserted on its MESSAGE, naming the limb and the figure,
never on the exception class alone (D-134). The case a naive implementation
misses is the long-serving employee on three days a week — so it has its own
test, two years in.

The figures are the rule set's (``family_resp_min_service_months``,
``family_resp_min_days_per_week``), never literals here or in the code.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta

from core.managers import tenant_context, tenant_context_of
from employees.models import WorkSchedule, WorkScheduleDay
from leave.accrual import FAMILY_RESPONSIBILITY_BASIS, accrue_employee, run_monthly_accrual
from leave.applications import FamilyResponsibilityIneligibleError, submit_application
from leave.cycles import ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveApplication, LeaveCycle, LeaveTransaction
from leave.tests.conftest import START

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType

# START is Sunday 1 March 2026. Four months on is 1 July 2026 (a Wednesday):
# on that day the employee has been employed EXACTLY four months, which is not
# "longer than" four months. The first eligible day is Thursday 2 July.
EXACTLY_FOUR_MONTHS = START + relativedelta(months=4)
FIRST_ELIGIBLE_DAY = EXACTLY_FOUR_MONTHS + datetime.timedelta(days=1)
TWO_YEARS_IN = datetime.date(2028, 3, 6)  # a Monday


def _schedule(employee, days_per_week: int):
    with tenant_context_of(employee):
        made = WorkSchedule.objects.create(
            tenant=employee.tenant,
            employee=employee,
            days_per_week=Decimal(days_per_week),
            ordinary_hours_per_week=Decimal(8 * days_per_week),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=employee.tenant,
                work_schedule=made,
                cycle_day=cycle_day,
                is_working_day=cycle_day < days_per_week,
                ordinary_hours=Decimal("8") if cycle_day < days_per_week else Decimal("0"),
            )
    return made


def _hold(employee, family_type, on_date, quantity="3.000"):
    """A balance to draw on, so a refusal can only be about eligibility."""
    with tenant_context_of(employee):
        cycle = ensure_cycles(employee, family_type, horizon=on_date)[-1]
        post_transaction(
            employee=employee,
            leave_cycle=cycle,
            leave_type=family_type,
            transaction_type=TransactionType.ACCRUAL,
            quantity=Decimal(quantity),
            unit=LeaveCycle.Unit.DAYS,
            transaction_date=cycle.cycle_start,
            calculation_basis="manual",
        )


def _application_count(employee):
    with tenant_context_of(employee):
        return LeaveApplication.objects.filter(employee=employee).count()


# ------------------------------------------------------------ the application


def test_two_years_service_on_three_days_a_week_is_refused_on_the_days_limb(
    employee, engagement, leave_rules, family_type
):
    """THE CASE A NAIVE IMPLEMENTATION MISSES: service is long past four
    months, so only the days-a-week limb can refuse it."""
    _schedule(employee, 3)
    _hold(employee, family_type, TWO_YEARS_IN)

    with pytest.raises(FamilyResponsibilityIneligibleError) as raised:
        submit_application(
            employee, leave_type=family_type, start_date=TWO_YEARS_IN, end_date=TWO_YEARS_IN
        )

    message = str(raised.value)
    assert "works 3 days a week, and BCEA s27(1)(b) requires at least 4" in message, message
    assert "family_resp_min_days_per_week" in message
    assert "s27(1)(a)" not in message, "service is not the failing limb here."
    assert _application_count(employee) == 0, "a refusal writes nothing."


def test_under_four_months_service_on_five_days_a_week_is_refused_on_the_service_limb(
    employee, engagement, leave_rules, family_type, schedule_5day
):
    on_date = datetime.date(2026, 5, 4)  # a Monday, two months in
    _hold(employee, family_type, on_date)

    with pytest.raises(FamilyResponsibilityIneligibleError) as raised:
        submit_application(employee, leave_type=family_type, start_date=on_date, end_date=on_date)

    message = str(raised.value)
    assert "employed since 01 March 2026" in message, message
    assert "BCEA s27(1)(a) requires longer than 4 months" in message, message
    assert "eligible from 02 July 2026" in message, message
    assert "family_resp_min_service_months" in message
    assert "s27(1)(b)" not in message, "days a week is not the failing limb here."
    assert _application_count(employee) == 0


def test_exactly_four_months_is_not_longer_than_four_months(
    employee, engagement, leave_rules, family_type, schedule_5day
):
    """The boundary is the statute's word, "longer than". On the day service
    reaches exactly four months the employee does not yet qualify; the next
    day they do."""
    _hold(employee, family_type, EXACTLY_FOUR_MONTHS)

    with pytest.raises(FamilyResponsibilityIneligibleError) as raised:
        submit_application(
            employee,
            leave_type=family_type,
            start_date=EXACTLY_FOUR_MONTHS,
            end_date=EXACTLY_FOUR_MONTHS,
        )
    assert "BCEA s27(1)(a) requires longer than 4 months" in str(raised.value)

    application = submit_application(
        employee,
        leave_type=family_type,
        start_date=FIRST_ELIGIBLE_DAY,
        end_date=FIRST_ELIGIBLE_DAY,
    )
    assert application.status == LeaveApplication.Status.SUBMITTED


def test_failing_both_limbs_names_both(employee, engagement, leave_rules, family_type):
    on_date = datetime.date(2026, 4, 6)  # a Monday, one month in
    _schedule(employee, 3)
    _hold(employee, family_type, on_date)

    with pytest.raises(FamilyResponsibilityIneligibleError) as raised:
        submit_application(employee, leave_type=family_type, start_date=on_date, end_date=on_date)

    message = str(raised.value)
    assert "BCEA s27(1)(a) requires longer than 4 months" in message, message
    assert "works 3 days a week, and BCEA s27(1)(b) requires at least 4" in message, message


def test_an_eligible_employee_is_not_refused(
    employee, engagement, leave_rules, family_type, schedule_5day
):
    """The negative half: both limbs met, the application goes through."""
    _hold(employee, family_type, TWO_YEARS_IN)

    application = submit_application(
        employee, leave_type=family_type, start_date=TWO_YEARS_IN, end_date=TWO_YEARS_IN
    )
    assert application.status == LeaveApplication.Status.SUBMITTED
    assert application.total_days == Decimal("1.000")


def test_eligibility_is_checked_only_for_family_responsibility(
    employee, engagement, leave_rules, annual_type
):
    """A three-day-a-week employee a month in may still apply for ANNUAL
    leave — s27(1) governs s27 leave and nothing else."""
    on_date = datetime.date(2026, 4, 6)
    _schedule(employee, 3)

    application = submit_application(
        employee, leave_type=annual_type, start_date=on_date, end_date=on_date
    )
    assert application.pk is not None


def test_the_figures_come_from_the_rule_set_not_the_code(
    employee, engagement, leave_rules, family_type
):
    """Move both figures in the rule set and the refusal moves with them: a
    literal 4 anywhere in the check would leave this test refused."""
    leave_rules.family_resp_min_days_per_week = 3
    leave_rules.family_resp_min_service_months = 1
    leave_rules.save()
    on_date = datetime.date(2026, 4, 6)  # one month and five days in
    _schedule(employee, 3)
    _hold(employee, family_type, on_date)

    application = submit_application(
        employee, leave_type=family_type, start_date=on_date, end_date=on_date
    )
    assert application.status == LeaveApplication.Status.SUBMITTED


# ----------------------------------------------------------------- the grant


def test_the_grant_waits_for_eligibility_and_is_dated_the_day_it_exists(
    employee, engagement, leave_rules, family_type, schedule_5day, working_time_rules
):
    """No balance is shown for leave the employee does not yet have. Before
    four months nothing is granted; once eligible the grant is dated the first
    eligible day, not backdated to cycle start."""
    with tenant_context_of(employee):
        assert accrue_employee(employee, family_type, as_at=datetime.date(2026, 5, 31)) is None
        txn = accrue_employee(employee, family_type, as_at=datetime.date(2026, 7, 31))

    assert txn is not None
    assert txn.calculation_basis == FAMILY_RESPONSIBILITY_BASIS
    assert txn.transaction_date == FIRST_ELIGIBLE_DAY
    assert txn.days == Decimal(leave_rules.family_responsibility_days)


def test_a_three_day_a_week_employee_is_never_granted_and_the_run_completes(
    employer, employee, engagement, leave_rules, family_type, working_time_rules
):
    """Ineligibility is the ordinary monthly state of a recent hire, not a
    defect: the employer's accrual run completes and simply grants nothing."""
    _schedule(employee, 3)

    run = run_monthly_accrual(employer, family_type, datetime.date(2028, 3, 31))

    assert run.status == run.Status.COMPLETED
    assert run.transactions_created == 0
    with tenant_context(employee.tenant_id):
        assert not LeaveTransaction.objects.filter(leave_type=family_type).exists()
