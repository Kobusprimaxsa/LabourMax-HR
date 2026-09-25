"""``public_holiday_observance`` — task 4, task 6, and D-319.

THIS CHANGES CHUNK 2'S ANSWER, and that is the point: an employer that
EXCHANGED a statutory public holiday (PHA s2(2)) works it as ordinary, and
that day IS deducted from leave — on the exact same calendar date a different
employer's employee still gets it off, unpaid-by-leave, for free.

D-319 split what "not observed" meant. EXCHANGED makes the day ordinary;
WORKED BY AGREEMENT (BCEA s18(1)) leaves it a public holiday, so leave over
it is NOT charged. The rows below that used to say is_observed=False meant an
exchange, and now say so.
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


def test_an_employer_who_exchanged_it_works_it_as_ordinary_and_it_is_deducted(
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
            name="Exchanged",
            treatment=PublicHolidayObservance.Treatment.EXCHANGED,
        )

    _grant_balance(other_employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        other_employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert application.total_days == Decimal("5.000"), (
        "Exchanged: Wednesday is worked as ordinary, so all five days deduct."
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
            name="Exchanged",
            treatment=PublicHolidayObservance.Treatment.EXCHANGED,
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


# ================================================================= D-319

TREATMENT = PublicHolidayObservance.Treatment


def _observe(tenant, employer, day, treatment, *, holiday=None, name="Youth Day"):
    with tenant_context(tenant.pk):
        return PublicHolidayObservance.objects.create(
            tenant=tenant,
            employer=employer,
            public_holiday=holiday,
            observance_date=day,
            name=name,
            treatment=treatment,
        )


def test_a_holiday_worked_by_agreement_stays_a_holiday_so_leave_over_it_is_not_charged(
    other_employer, other_employee, leave_rules, annual_type, public_holiday_wednesday, tenant
):
    """BCEA s18(1): the day remains a public holiday. Four days, not five."""
    _observe(
        tenant,
        other_employer,
        HOLIDAY,
        TREATMENT.WORKED_BY_AGREEMENT,
        holiday=public_holiday_wednesday,
    )
    _grant_balance(other_employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        other_employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert application.total_days == Decimal("4.000")


def test_exchange_and_worked_by_agreement_on_the_same_date_for_two_employers(
    employee,
    engagement,
    leave_rules,
    annual_type,
    schedule_5day,
    public_holiday_wednesday,
    other_employer,
    other_employee,
    employer,
    tenant,
):
    """D-179's both-directions test, extended: the same Wednesday worked by
    agreement at one employer and exchanged at the other. Only the exchange
    charges leave."""
    _observe(
        tenant, employer, HOLIDAY, TREATMENT.WORKED_BY_AGREEMENT, holiday=public_holiday_wednesday
    )
    _observe(tenant, other_employer, HOLIDAY, TREATMENT.EXCHANGED, holiday=public_holiday_wednesday)
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))
    _grant_balance(other_employee, annual_type, quantity=Decimal("15.000"))

    worked = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )
    exchanged = submit_application(
        other_employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert (worked.total_days, exchanged.total_days) == (Decimal("4.000"), Decimal("5.000"))


def test_the_substitute_day_is_not_charged_either(
    employer,
    employee,
    engagement,
    leave_rules,
    annual_type,
    schedule_5day,
    tenant,
    public_holiday_wednesday,
):
    """The exchange is a PAIR: Wednesday becomes ordinary, Thursday the holiday."""
    thursday = datetime.date(2026, 3, 5)
    substitute = _observe(tenant, employer, thursday, TREATMENT.SUBSTITUTE, name="Exchanged")
    exchanged = _observe(
        tenant, employer, HOLIDAY, TREATMENT.EXCHANGED, holiday=public_holiday_wednesday
    )
    with tenant_context(tenant.pk):
        exchanged.substitute = substitute
        exchanged.full_clean()
        exchanged.save()
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=FRIDAY
    )

    assert application.total_days == Decimal("4.000")
    with tenant_context_of(employee):
        days = {
            d.leave_date: d.is_working_day
            for d in LeaveApplicationDay.objects.filter(leave_application=application)
        }
    assert (days[HOLIDAY], days[thursday]) == (True, False)


# ------------------------------------------------ Finding 3: the unpaired warning


def test_an_exchange_with_no_substitute_is_warned_naming_both_cases(
    employer, tenant, public_holiday_wednesday
):
    from leave.holidays import unpaired_exchange_warnings

    _observe(tenant, employer, HOLIDAY, TREATMENT.EXCHANGED, holiday=public_holiday_wednesday)
    with tenant_context(tenant.pk):
        (warning,) = unpaired_exchange_warnings(employer)
    assert "EXCHANGED for another day under the Public Holidays Act s2(2)" in warning
    assert "no substitute day is recorded" in warning
    assert "WORKED the holiday by agreement under BCEA s18(1)" in warning
    assert "at least double" in warning


def test_a_paired_exchange_and_a_worked_holiday_are_not_warned(
    employer, other_employer, tenant, public_holiday_wednesday
):
    from leave.holidays import unpaired_exchange_warnings

    substitute = _observe(tenant, employer, datetime.date(2026, 3, 5), TREATMENT.SUBSTITUTE)
    exchanged = _observe(
        tenant, employer, HOLIDAY, TREATMENT.EXCHANGED, holiday=public_holiday_wednesday
    )
    _observe(
        tenant,
        other_employer,
        HOLIDAY,
        TREATMENT.WORKED_BY_AGREEMENT,
        holiday=public_holiday_wednesday,
    )
    with tenant_context(tenant.pk):
        exchanged.substitute = substitute
        exchanged.save()
        assert unpaired_exchange_warnings(employer) == []
        assert unpaired_exchange_warnings(other_employer) == []


# ----------------------------------------------------------- the CHECKs, refusing


@pytest.mark.parametrize(
    "fields, constraint",
    [
        (
            {"treatment": "exchanged", "public_holiday": None},
            "observance_exchange_or_work_names_the_holiday",
        ),
        (
            {"treatment": "worked_by_agreement", "public_holiday": None},
            "observance_exchange_or_work_names_the_holiday",
        ),
        ({"treatment": "nonsense"}, "observance_treatment_is_known"),
    ],
)
def test_a_row_the_treatment_cannot_mean_is_refused_by_the_database(
    employer, tenant, fields, constraint
):
    from django.db import IntegrityError, transaction

    values = {
        "tenant": tenant,
        "employer": employer,
        "observance_date": HOLIDAY,
        "name": "x",
    } | fields
    with (
        pytest.raises(IntegrityError, match=constraint),
        transaction.atomic(),
        tenant_context(tenant.pk),
    ):
        PublicHolidayObservance.objects.create(**values)


def test_is_observed_cannot_drift_from_the_treatment(employer, tenant, public_holiday_wednesday):
    """Sheet 02's column is derived; the CHECK holds it, watched refusing."""
    from django.db import IntegrityError, transaction

    row = _observe(
        tenant, employer, HOLIDAY, TREATMENT.WORKED_BY_AGREEMENT, holiday=public_holiday_wednesday
    )
    assert row.is_observed is True
    with (
        pytest.raises(IntegrityError, match="observance_is_observed_follows_treatment"),
        transaction.atomic(),
        tenant_context(tenant.pk),
    ):
        PublicHolidayObservance.objects.filter(pk=row.pk).update(is_observed=False)
