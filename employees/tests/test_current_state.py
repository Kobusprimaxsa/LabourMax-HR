"""The as-at columns, and the night they are supposed to move.

The case this file exists for is the employee serving a month's notice. Their
termination is captured on 14 September to take effect on 31 October, and until
D-132 that capture closed the engagement, marked them terminated and stopped
billing for them on the spot — six weeks before they stopped working. Every
"current employee" query would then skip the person the December payroll still
owes a final salary, a leave payout and an IRP5, and nothing on any screen would
look wrong.

The other case is the mirror of D-107: a future-dated increase that has to move
the cache on 1 July with nobody touching the record.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from core.managers import tenant_context
from core.models import BackgroundJob, Tenant
from employees.currentstate import (
    JOB_NAME,
    engagement_in_force,
    refresh_all,
    refresh_current_state,
)
from employees.engagements import MINIMUM_AGE_PARAMETER, engage, terminate
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeEngagement
from employees.remuneration import MONTHLY_FACTOR_PARAMETER, capture
from employers.models import Employer, PayGroup
from statutory.models import MinimumWageRate, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)
NMW_HOURLY = Decimal("30.2300")


def make_id(sequence="5009"):
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


def today():
    return timezone.localdate()


@pytest.fixture
def parameters(db):
    for code, value, unit in (
        (MINIMUM_AGE_PARAMETER, "15.000000", StatutoryParameter.Unit.YEARS),
        (MONTHLY_FACTOR_PARAMETER, "4.333333", StatutoryParameter.Unit.RATIO),
    ):
        StatutoryParameter.objects.create(
            parameter_code=code,
            value_numeric=Decimal(value),
            unit=unit,
            effective_from=datetime.date(1997, 12, 1),
            source_reference="Basic Conditions of Employment Act 75 of 1997",
        )


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def minimum_wage(db, sector):
    return MinimumWageRate.objects.create(
        sector=None,
        hourly_rate=NMW_HOURLY,
        effective_from=START,
        source_reference="GN R.7083 in Government Gazette 54075, 3 February 2026",
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Subscriber")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)


@pytest.fixture
def pay_group(db, tenant, employer):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly staff",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=START,
        )


@pytest.fixture
def weekly_group(db, tenant, employer):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Weekly cleaners",
            pay_frequency=PayGroup.PayFrequency.WEEKLY,
            first_period_start=START,
        )


def make_employee(tenant, employer, *, sequence="5009", first="Thandi", last="Mokoena"):
    with tenant_context(tenant.pk):
        return Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name=first,
            last_name=last,
            date_of_birth=BORN,
            mobile_number=f"+2782000{sequence}",
            email=f"{first.lower()}@example.com",
            id_number=make_id(sequence),
        )


@pytest.fixture
def employee(db, tenant, employer, parameters):
    person = make_employee(tenant, employer)
    engage(person, start_date=START, job_title="Domestic worker")
    return person


def reload(employee):
    with tenant_context(employee.tenant_id):
        return Employee.objects.get(pk=employee.pk)


# ------------------------------------------- the employee who is serving notice


def test_a_future_termination_leaves_the_employee_current_active_and_billable(employee):
    """THE ONE THAT DROPS SOMEBODY FROM THEIR OWN FINAL PAYSLIP.

    Captured today, effective in six weeks. Until the date arrives this person is
    at work, is owed a salary, and is using the subscription.
    """
    leaving = today() + datetime.timedelta(days=45)
    engagement = terminate(
        _only(employee),
        termination_date=leaving,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    fresh = reload(employee)
    assert fresh.status == Employee.Status.ACTIVE
    assert fresh.is_billable is True
    assert fresh.latest_termination_date == leaving, "The fact is recorded immediately."

    with tenant_context(employee.tenant_id):
        engagement.refresh_from_db()
    assert engagement.is_current is True
    assert engagement.termination_date == leaving


def test_the_night_the_termination_date_arrives_moves_everything(employee):
    leaving = today() + datetime.timedelta(days=45)
    engagement = terminate(
        _only(employee),
        termination_date=leaving,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    refresh_current_state(employee, on_date=leaving + datetime.timedelta(days=1))

    fresh = reload(employee)
    assert fresh.status == Employee.Status.TERMINATED
    assert fresh.is_billable is False
    with tenant_context(employee.tenant_id):
        engagement.refresh_from_db()
    assert engagement.is_current is False


def test_the_last_day_of_service_is_still_service(employee):
    """termination_date is the LAST DAY, inclusive. Read as exclusive it costs the
    employee a day of pay and makes the day look unworked rather than unpaid."""
    leaving = today() + datetime.timedelta(days=10)
    terminate(
        _only(employee),
        termination_date=leaving,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    assert engagement_in_force(employee, leaving) is not None
    assert engagement_in_force(employee, leaving + datetime.timedelta(days=1)) is None


def test_a_termination_dated_today_takes_effect_at_once(employee):
    """No waiting for the night. Somebody dismissed today is terminated today."""
    terminate(
        _only(employee),
        termination_date=today(),
        reason_code=EmployeeEngagement.TerminationReason.DISMISSAL_MISCONDUCT,
    )

    fresh = reload(employee)
    assert fresh.status == Employee.Status.ACTIVE, (
        "Still the last day of service — they are employed until midnight."
    )

    refresh_current_state(employee, on_date=today() + datetime.timedelta(days=1))
    assert reload(employee).status == Employee.Status.TERMINATED


# ---------------------------------------------------- engagements not yet begun


def test_an_engagement_booked_for_next_month_leaves_the_employee_in_draft(
    tenant, employer, parameters
):
    person = make_employee(tenant, employer, sequence="5011", first="Sipho", last="Ndlovu")
    starting = today() + datetime.timedelta(days=30)
    engagement = engage(person, start_date=starting, job_title="Domestic worker")

    fresh = reload(person)
    assert fresh.status == Employee.Status.DRAFT, "They have not started yet."
    assert fresh.first_engagement_date == starting, "But the date is on the record."
    assert engagement.is_current is False

    refresh_current_state(person, on_date=starting)
    assert reload(person).status == Employee.Status.ACTIVE
    with tenant_context(tenant.pk):
        engagement.refresh_from_db()
    assert engagement.is_current is True


def test_a_rehire_captured_in_advance_is_not_refused(employee):
    """The open-engagement guard reads the termination date, not is_current (D-132).

    Reading is_current would refuse every re-hire arranged before the current one
    has actually ended — which is how a re-hire is usually arranged.
    """
    leaving = today() + datetime.timedelta(days=30)
    terminate(
        _only(employee),
        termination_date=leaving,
        reason_code=EmployeeEngagement.TerminationReason.END_OF_CONTRACT,
    )

    returning = leaving + datetime.timedelta(days=60)
    second = engage(employee, start_date=returning, job_title="Domestic worker")

    assert second.engagement_number == 2
    with tenant_context(employee.tenant_id):
        assert EmployeeEngagement.objects.filter(employee=employee).count() == 2
        assert EmployeeEngagement.objects.filter(employee=employee, is_current=True).count() == 1


def test_the_flag_moves_between_two_engagements_without_tripping_the_unique(employee):
    """The outgoing engagement has to stop being current BEFORE the incoming one
    starts. Both in one transaction, and the partial unique is watching."""
    leaving = today() + datetime.timedelta(days=30)
    first = _only(employee)
    terminate(
        first,
        termination_date=leaving,
        reason_code=EmployeeEngagement.TerminationReason.END_OF_CONTRACT,
    )
    returning = leaving + datetime.timedelta(days=1)
    second = engage(employee, start_date=returning, job_title="Domestic worker")

    change = refresh_current_state(employee, on_date=returning)

    assert change.engagements_moved == 2
    with tenant_context(employee.tenant_id):
        first.refresh_from_db()
        second.refresh_from_db()
    assert first.is_current is False
    assert second.is_current is True


# --------------------------------------------------- statuses set by other work


def test_an_employee_on_leave_is_left_on_leave(employee):
    """The leave engine owns that status; an engagement in force says nothing
    about whether somebody is at their desk today."""
    with tenant_context(employee.tenant_id):
        Employee.objects.filter(pk=employee.pk).update(status=Employee.Status.ON_LEAVE)

    refresh_current_state(reload(employee), on_date=today())
    assert reload(employee).status == Employee.Status.ON_LEAVE


def test_service_ending_while_suspended_still_terminates(employee):
    """Otherwise a suspended employee whose service ended stays billable forever."""
    leaving = today() - datetime.timedelta(days=1)
    engagement = _only(employee)
    with tenant_context(employee.tenant_id):
        EmployeeEngagement.objects.filter(pk=engagement.pk).update(
            termination_date=leaving,
            termination_reason_code=EmployeeEngagement.TerminationReason.DISMISSAL_MISCONDUCT,
            is_current=False,
        )
        Employee.objects.filter(pk=employee.pk).update(status=Employee.Status.SUSPENDED)

    refresh_current_state(reload(employee), on_date=today())

    fresh = reload(employee)
    assert fresh.status == Employee.Status.TERMINATED
    assert fresh.is_billable is False


def test_an_archived_employee_is_never_reactivated(employee):
    """Archiving is terminal, and it is the retention policy's to undo."""
    with tenant_context(employee.tenant_id):
        Employee.objects.filter(pk=employee.pk).update(status=Employee.Status.ARCHIVED)

    refresh_current_state(reload(employee), on_date=today())
    assert reload(employee).status == Employee.Status.ARCHIVED


# ------------------------------------------------------------- the pay cache


def test_the_job_moves_a_future_dated_increase_on_its_own_date(
    employee, pay_group, weekly_group, minimum_wage
):
    """D-107 from the other side: this is the job the write path deliberately
    leaves work for."""
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        as_at=START,
    )
    capture(
        employee,
        pay_basis="weekly",
        rate_amount=Decimal("1600"),
        effective_from=datetime.date(2026, 7, 1),
        pay_group=weekly_group,
        as_at=datetime.date(2026, 3, 15),
    )

    assert reload(employee).current_pay_basis == "monthly"

    refresh_current_state(reload(employee), on_date=datetime.date(2026, 7, 1))

    fresh = reload(employee)
    assert fresh.current_pay_basis == "weekly"
    assert fresh.current_pay_group_id == weekly_group.pk


# --------------------------------------------------------------- the whole run


def test_the_run_is_idempotent(employee, pay_group, minimum_wage):
    """Twice in one night, or a catch-up after a missed one, changes nothing twice."""
    capture(
        employee,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        as_at=START,
    )
    first = refresh_all(on_date=today(), record_job=False)
    second = refresh_all(on_date=today(), record_job=False)

    assert second.employees_changed == 0
    assert second.engagements_moved == 0
    assert first.employees == second.employees == 1


def test_a_dry_run_writes_nothing(employee):
    leaving = today() - datetime.timedelta(days=1)
    with tenant_context(employee.tenant_id):
        EmployeeEngagement.objects.filter(employee=employee).update(
            termination_date=leaving,
            termination_reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        )

    dry = refresh_all(on_date=today(), dry_run=True, record_job=False)
    assert dry.employees_changed == 1
    assert reload(employee).status == Employee.Status.ACTIVE, "Still untouched."

    wet = refresh_all(on_date=today(), record_job=False)
    assert wet.employees_changed == 1
    assert reload(employee).status == Employee.Status.TERMINATED


def test_one_tenant_cannot_be_refreshed_into_another(db, sector, parameters):
    """The job enters each tenant's context rather than reaching across them.

    D-54: a strict tenant table has no platform override, so there is nothing to
    reach across with — and a maintenance job that could see every employer at
    once is the hole platform_context() exists to keep singular.
    """
    first = Tenant.objects.create(trading_name="One")
    second = Tenant.objects.create(trading_name="Two")
    with tenant_context(first.pk):
        employer_one = Employer.objects.create(tenant=first, trading_name="A", sector=sector)
    with tenant_context(second.pk):
        employer_two = Employer.objects.create(tenant=second, trading_name="B", sector=sector)

    person_one = make_employee(first, employer_one, sequence="5030", first="Ann", last="Adams")
    person_two = make_employee(second, employer_two, sequence="5031", first="Bea", last="Brown")
    engage(person_one, start_date=START, job_title="Domestic worker")
    engage(person_two, start_date=START, job_title="Domestic worker")

    summary = refresh_all(on_date=today(), tenant_ids=[first.pk], record_job=False)

    assert summary.tenants == 1
    assert summary.employees == 1, "Only the tenant asked for was read."


def test_the_run_records_a_background_job(employee):
    """A missed night has to be visible without reading broker internals."""
    refresh_all(on_date=today())

    job = BackgroundJob.objects.filter(job_name=JOB_NAME).order_by("-queued_at").first()
    assert job is not None
    assert job.status == BackgroundJob.Status.SUCCEEDED
    assert job.result_summary["employees"] == 1
    assert job.finished_at is not None


def test_the_command_runs_and_reports(employee, capsys):
    call_command("refreshemployeecache", "--no-job-record")
    out = capsys.readouterr().out
    assert "employees read" in out


def test_the_command_refuses_a_date_it_cannot_read(employee):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError) as raised:
        call_command("refreshemployeecache", "--as-at", "1 July 2026")
    assert "ISO date" in str(raised.value)


def _only(employee) -> EmployeeEngagement:
    with tenant_context(employee.tenant_id):
        return EmployeeEngagement.objects.filter(employee=employee).order_by("-engagement_number")[
            0
        ]
