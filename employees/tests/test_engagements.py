"""Engagements, re-hire, and the minimum age.

The case this file exists for is the re-hire. An employee who left in 2027 and came
back in 2029 has TWO periods of service, not one four-year period — and if the
second engagement is captured by editing the first, every downstream figure is wrong
in the employee's favour and every one of them looks entirely plausible: notice,
leave accrual, the BCEA's six-month thresholds, severance.

The other case is the minimum age, where the failure is not a wrong number but an
offence under BCEA s43(3) committed by the employer, with this system having
recorded it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import (
    MINIMUM_AGE_PARAMETER,
    EngagementRefusedError,
    check_minimum_age,
    continuous_service_days,
    engage,
    terminate,
)
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeEngagement, EmployeePosition
from employers.models import Employer
from statutory.models import Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)


def make_id(sequence="5009"):
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def minimum_age(db):
    """The BCEA s43 threshold, as reference data rather than as a literal."""
    return StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("15.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s43(1)",
    )


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Subscriber")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)


@pytest.fixture
def employee(db, tenant, employer):
    with tenant_context(tenant.pk):
        return Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=BORN,
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=make_id(),
        )


# ----------------------------------------------------------------- engaging


def test_engaging_creates_an_engagement_and_an_opening_position(employee, minimum_age):
    engagement = engage(employee, start_date=START, job_title="Domestic worker")

    assert engagement.engagement_number == 1
    assert engagement.is_current is True
    with tenant_context(employee.tenant_id):
        position = EmployeePosition.objects.get(engagement=engagement)
    assert position.effective_from == START
    assert position.change_reason == EmployeePosition.ChangeReason.NEW_ENGAGEMENT


def test_engaging_activates_a_draft_employee_and_records_the_first_date(employee, minimum_age):
    assert employee.status == Employee.Status.DRAFT
    engage(employee, start_date=START, job_title="Domestic worker")

    with tenant_context(employee.tenant_id):
        employee.refresh_from_db()
    assert employee.status == Employee.Status.ACTIVE
    assert employee.first_engagement_date == START


def test_a_second_open_engagement_is_refused(employee, minimum_age):
    """Two open engagements count service twice, for notice, leave and severance."""
    engage(employee, start_date=START, job_title="Domestic worker")

    with pytest.raises(EngagementRefusedError) as caught:
        engage(employee, start_date=datetime.date(2026, 6, 1), job_title="Domestic worker")
    assert "already engaged" in str(caught.value)


def test_the_partial_unique_backs_the_service_function_up(employee, minimum_age):
    """The database says it too, for the writer that does not come through engage()."""
    engage(employee, start_date=START, job_title="Domestic worker")

    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(employee.tenant_id):
        EmployeeEngagement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement_number=2,
            start_date=datetime.date(2026, 6, 1),
            is_current=True,
        )


# ------------------------------------------------------------------- re-hire


def test_a_rehire_is_a_second_engagement_not_an_edited_first(employee, minimum_age):
    """THE ONE THAT GRANTS SERVICE NOBODY EARNED.

    Left in 2027, back in 2029. Two periods, not one four-year period. Editing the
    first engagement's start date would hand this employee a longer notice period,
    more accrued leave and a larger severance figure — and nothing on any screen
    would look wrong.
    """
    first = engage(employee, start_date=START, job_title="Domestic worker")
    terminate(
        first,
        termination_date=datetime.date(2027, 2, 28),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    second = engage(employee, start_date=datetime.date(2029, 3, 1), job_title="Domestic worker")

    assert second.engagement_number == 2
    assert second.pk != first.pk
    with tenant_context(employee.tenant_id):
        first.refresh_from_db()
        assert first.start_date == START, "The first engagement must not have been touched."
        assert EmployeeEngagement.objects.filter(employee=employee).count() == 2


def test_service_counts_from_the_current_engagement_only(employee, minimum_age):
    """A break in employment breaks continuous service. That is the BCEA's rule and
    the reason the function is named for it."""
    first = engage(employee, start_date=START, job_title="Domestic worker")
    terminate(
        first,
        termination_date=datetime.date(2027, 2, 28),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )
    engage(employee, start_date=datetime.date(2029, 3, 1), job_title="Domestic worker")

    with tenant_context(employee.tenant_id):
        days = continuous_service_days(employee, datetime.date(2029, 3, 31))

    assert days == 31, "31 days into the SECOND engagement, not three years and change."


def test_a_rehire_starting_inside_a_previous_period_is_refused(employee, minimum_age):
    first = engage(employee, start_date=START, job_title="Domestic worker")
    terminate(
        first,
        termination_date=datetime.date(2027, 2, 28),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    with pytest.raises(EngagementRefusedError) as caught:
        engage(employee, start_date=datetime.date(2027, 1, 1), job_title="Domestic worker")
    assert "double-count" in str(caught.value)


# --------------------------------------------------------------- termination


def test_terminating_closes_the_engagement_and_the_position(employee, minimum_age):
    engagement = engage(employee, start_date=START, job_title="Domestic worker")
    terminate(
        engagement,
        termination_date=datetime.date(2026, 8, 31),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        notice_worked=True,
    )

    with tenant_context(employee.tenant_id):
        engagement.refresh_from_db()
        position = EmployeePosition.objects.get(engagement=engagement)
        employee.refresh_from_db()

    assert engagement.is_current is False
    assert position.effective_to == datetime.date(2026, 9, 1), (
        "effective_to is EXCLUSIVE, so the position ends the day after the last day "
        "of service - otherwise the final day has no position and no job grade."
    )
    assert employee.status == Employee.Status.TERMINATED
    assert employee.is_billable is False, "D-33: terminated employees are not billable."


def test_only_retrenchment_triggers_severance(employee, minimum_age):
    """BCEA s41, stated once on the model rather than restated in the engine."""
    engagement = engage(employee, start_date=START, job_title="Domestic worker")
    terminate(
        engagement,
        termination_date=datetime.date(2026, 8, 31),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )
    assert engagement.triggers_severance is False

    engagement.termination_reason_code = EmployeeEngagement.TerminationReason.RETRENCHMENT
    assert engagement.triggers_severance is True


def test_a_termination_without_a_reason_is_refused(employee, minimum_age):
    engagement = engage(employee, start_date=START, job_title="Domestic worker")
    with pytest.raises(EngagementRefusedError) as caught:
        terminate(engagement, termination_date=datetime.date(2026, 8, 31), reason_code="")
    assert "severance" in str(caught.value)


def test_the_database_refuses_a_termination_with_no_reason_too(employee, minimum_age):
    engagement = engage(employee, start_date=START, job_title="Domestic worker")
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(employee.tenant_id):
        EmployeeEngagement.objects.filter(pk=engagement.pk).update(
            termination_date=datetime.date(2026, 8, 31), is_current=False
        )


# --------------------------------------------------------------- minimum age


def test_a_child_cannot_be_engaged(tenant, employer, minimum_age):
    """BCEA s43(1) and s43(3). Not a warning — an offence, so the record is refused."""
    with tenant_context(tenant.pk):
        child = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Too",
            last_name="Young",
            date_of_birth=datetime.date(2012, 6, 1),
            mobile_number="+27820000050",
            email="ty@example.com",
            id_number=make_id(sequence="5020"),
            id_type=Employee.IdType.PASSPORT,
        )

    with pytest.raises(EngagementRefusedError) as caught:
        engage(child, start_date=START, job_title="Domestic worker")

    assert "s43(1)" in str(caught.value)
    assert "13" in str(caught.value), "The message names the age it computed."

    with tenant_context(tenant.pk):
        assert EmployeeEngagement.objects.filter(employee=child).count() == 0
        assert EmployeePosition.objects.filter(employee=child).count() == 0


def test_the_day_the_minimum_age_is_reached_is_permitted(minimum_age):
    """A boundary that must be inclusive. Fifteen exactly is fifteen."""
    born = datetime.date(2011, 3, 1)
    assert check_minimum_age(born, datetime.date(2026, 2, 28)).permitted is False
    assert check_minimum_age(born, datetime.date(2026, 3, 1)).permitted is True


def test_the_threshold_is_read_from_reference_data_not_from_code(minimum_age):
    """Change the DATA and the rule changes. That is the point of D-100.

    If the school-leaving age moved to 16, superseding one row would be the whole
    change — no release, no code edit, and every historic engagement still resolves
    against the figure that applied on its own start date.
    """
    StatutoryParameter.objects.filter(pk=minimum_age.pk).update(
        effective_to=datetime.date(2027, 3, 1)
    )
    StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("16.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(2027, 3, 1),
        source_reference="Hypothetical amendment, for the test",
    )

    # Born June 2011, so 15 on both dates below and 16 on neither. The same person
    # and the same age; only the figure in force differs.
    born = datetime.date(2011, 6, 1)
    assert check_minimum_age(born, datetime.date(2027, 2, 1)).permitted is True
    assert check_minimum_age(born, datetime.date(2027, 3, 1)).permitted is False


def test_an_unloaded_threshold_refuses_rather_than_assuming_one(db):
    """The opposite of how PayGroup.clean() handles missing reference data.

    There, validation is skipped because the staleness guard blocks the payroll run
    further along. Here there is no backstop, and the failure is not a wrong figure
    on a payslip — it is a child in employment. So it refuses, and names the command.
    """
    with pytest.raises(EngagementRefusedError) as caught:
        check_minimum_age(BORN, START)
    assert "loadstatutory" in str(caught.value)
    assert "s43(3)" in str(caught.value)


def test_the_age_is_computed_on_the_start_date_not_today(minimum_age):
    """A back-dated engagement is judged on the date it began.

    Reading the clock instead would let a 2020 engagement for a then-14-year-old pass
    today, because they are eighteen now.
    """
    born = datetime.date(2010, 1, 1)
    assert check_minimum_age(born, datetime.date(2024, 1, 1)).permitted is False
    assert check_minimum_age(born, datetime.date(2025, 1, 1)).permitted is True


# ----------------------------------------------------------------- positions


def test_a_promotion_inserts_a_row_and_does_not_overwrite(employee, minimum_age):
    """job_grade selects the minimum wage row, so March must keep March's grade."""
    engagement = engage(employee, start_date=START, job_title="Cleaner")

    with tenant_context(employee.tenant_id):
        EmployeePosition.objects.filter(engagement=engagement, effective_to__isnull=True).update(
            effective_to=datetime.date(2026, 7, 1)
        )
        EmployeePosition.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement=engagement,
            job_title="Supervisor",
            effective_from=datetime.date(2026, 7, 1),
            change_reason=EmployeePosition.ChangeReason.PROMOTION,
        )
        titles = list(
            EmployeePosition.objects.filter(employee=employee)
            .order_by("effective_from")
            .values_list("job_title", flat=True)
        )

    assert titles == ["Cleaner", "Supervisor"]


def test_two_positions_cannot_be_in_force_at_once(employee, minimum_age):
    """THE ONE THE UNIQUE CONSTRAINT DOES NOT CATCH.

    A unique on (employee, effective_from) stops two positions STARTING on one day.
    A back-dated promotion starts INSIDE the open period and slips straight past it —
    leaving two job grades in force, so two minimum wages, with the ORM picking one
    by ordering. Only the exclusion constraint sees it.
    """
    engagement = engage(employee, start_date=START, job_title="Cleaner")

    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(employee.tenant_id):
        EmployeePosition.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement=engagement,
            job_title="Supervisor",
            effective_from=datetime.date(2026, 6, 1),
            change_reason=EmployeePosition.ChangeReason.PROMOTION,
        )


# ------------------------------------------------------------ fixed term


def test_a_fixed_term_contract_needs_an_end_date(employee, minimum_age):
    """LRA s198B turns an unterminated fixed term into permanent employment after
    three months, so a blank end date is a liability rather than a missing field."""
    engagement = EmployeeEngagement(
        tenant=employee.tenant,
        employee=employee,
        start_date=START,
        contract_type=EmployeeEngagement.ContractType.FIXED_TERM,
    )
    with pytest.raises(ValidationError) as caught:
        engagement.full_clean(exclude=["tenant"])
    assert "s198B" in str(caught.value)


def test_the_database_refuses_a_fixed_term_with_no_end_date(employee, minimum_age):
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(employee.tenant_id):
        EmployeeEngagement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            start_date=START,
            contract_type=EmployeeEngagement.ContractType.FIXED_TERM,
        )
