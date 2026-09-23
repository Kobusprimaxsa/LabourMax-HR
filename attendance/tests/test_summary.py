"""timesheet_summary — a cache, and the two-layer staleness that guards it.

Task 1a: every guard here is proven to fail before it is trusted — the
staleness signal is watched catching a real change, and the ground-truth
function is watched catching a flag that was cleared by hand.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.capture import capture
from attendance.models import AttendanceDay, TimesheetSummary
from attendance.summary import is_actually_stale, recompute_summary
from calculators.attendance import DayType
from core.managers import tenant_context
from core.models import Tenant
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from employers.models import Employer, PayGroup
from payroll.models import PayPeriod
from statutory.models import PublicHoliday, Sector, TaxYear, WorkingTimeRuleSet

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
END = datetime.date(2026, 3, 31)
MONDAY = datetime.date(2026, 3, 2)  # a Monday, an ordinary working day
SUNDAY = datetime.date(2026, 3, 8)  # a Sunday, not ordinarily worked


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Household")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)


@pytest.fixture
def tax_year(db):
    return TaxYear.objects.create(
        label="2026/2027", start_date=START, end_date=datetime.date(2027, 2, 28)
    )


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
def pay_period(db, tenant, pay_group, tax_year):
    with tenant_context(tenant.pk):
        return PayPeriod.objects.create(
            tenant=tenant,
            pay_group=pay_group,
            tax_year=tax_year,
            period_number=1,
            period_start=START,
            period_end=END,
            payment_date=datetime.date(2026, 4, 1),
        )


@pytest.fixture
def employee(db, tenant, employer, pay_group):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number="9001015009086",
        )
        person.current_pay_group = pay_group
        person.save(update_fields=["current_pay_group", "updated_at"])
        return person


@pytest.fixture
def rules(db):
    return WorkingTimeRuleSet.objects.create(
        sector=None,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Test fixture",
        ordinary_hours_per_week=Decimal("45"),
        ordinary_hours_per_day_5day=Decimal("9"),
        ordinary_hours_per_day_6day=Decimal("8"),
        overtime_multiplier=Decimal("1.5"),
        max_overtime_hours_per_day=Decimal("3"),
        max_overtime_hours_per_week=Decimal("10"),
        sunday_multiplier_ordinary=Decimal("1.5"),
        sunday_multiplier_non_ordinary=Decimal("2.0"),
        public_holiday_worked_multiplier=Decimal("2.0"),
        public_holiday_not_worked_paid=True,
        night_work_start_time=datetime.time(18, 0),
        night_work_end_time=datetime.time(6, 0),
        night_allowance_type="percentage",
        night_allowance_value=Decimal("10"),
        standby_allowance_per_shift=Decimal("50.00"),
        standby_window_start=datetime.time(18, 0),
        standby_window_end=datetime.time(6, 0),
        standby_hours_before_overtime=Decimal("2"),
        min_paid_hours_per_day=Decimal("6"),
        meal_interval_after_hours=Decimal("5"),
        meal_interval_minutes=60,
        daily_rest_hours=12,
        weekly_rest_hours=36,
        accommodation_deduction_capped=True,
        accommodation_deduction_max_pct=Decimal("10"),
    )


@pytest.fixture
def schedule(db, tenant, employee):
    with tenant_context(tenant.pk):
        made = WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=made,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,  # Monday-Friday
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
        return made


def test_recompute_sums_the_days(employee, tenant, pay_period, rules, schedule):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    capture(
        employee,
        work_date=datetime.date(2026, 3, 3),
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(18, 0),
        unpaid_break_minutes=60,
    )

    summary = recompute_summary(employee, pay_period)

    with tenant_context(tenant.pk):
        days = list(AttendanceDay.objects.filter(employee=employee))
    assert summary.total_ordinary_hours == sum((d.ordinary_hours for d in days), Decimal(0))
    assert summary.total_overtime_hours == sum((d.overtime_hours for d in days), Decimal(0))
    assert summary.is_stale is False


def test_recompute_is_idempotent(employee, tenant, pay_period, rules, schedule):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    first = recompute_summary(employee, pay_period)
    second = recompute_summary(employee, pay_period)

    with tenant_context(tenant.pk):
        assert TimesheetSummary.objects.filter(employee=employee).count() == 1
    assert first.total_ordinary_hours == second.total_ordinary_hours
    assert first.total_days_worked == second.total_days_worked


def test_changing_a_day_marks_the_summary_stale_through_the_signal(
    employee, tenant, pay_period, rules, schedule
):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    summary = recompute_summary(employee, pay_period)
    assert summary.is_stale is False  # PROVE THE GUARD CAN BE FALSE first

    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(18, 0),
        unpaid_break_minutes=60,
    )

    with tenant_context(tenant.pk):
        summary.refresh_from_db()
    assert summary.is_stale is True


def test_deleting_a_day_marks_the_summary_stale_through_the_signal(
    employee, tenant, pay_period, rules, schedule
):
    day = capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    summary = recompute_summary(employee, pay_period)
    assert summary.is_stale is False

    with tenant_context(tenant.pk):
        day.delete()
        summary.refresh_from_db()
    assert summary.is_stale is True


def test_a_flag_cleared_by_hand_is_still_detectably_stale(
    employee, tenant, pay_period, rules, schedule
):
    """PROVE THE GUARD FAILS: is_actually_stale must not simply trust is_stale."""
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    summary = recompute_summary(employee, pay_period)

    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(18, 0),
        unpaid_break_minutes=60,
    )
    with tenant_context(tenant.pk):
        TimesheetSummary.objects.filter(pk=summary.pk).update(is_stale=False)
        summary.refresh_from_db()

    assert summary.is_stale is False  # the flag was cleared
    assert is_actually_stale(summary) is True  # but the data says otherwise


def test_is_actually_stale_is_false_when_nothing_changed(
    employee, tenant, pay_period, rules, schedule
):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    summary = recompute_summary(employee, pay_period)

    assert is_actually_stale(summary) is False


def test_a_public_holiday_on_an_ordinary_working_day_counts_not_worked(
    employee, tenant, pay_period, rules, schedule
):
    PublicHoliday.objects.create(
        holiday_date=MONDAY,
        name="Human Rights Day (test)",
        is_statutory=True,
        source_reference="Test fixture",
    )

    summary = recompute_summary(employee, pay_period)

    assert summary.total_public_holidays_not_worked == Decimal("1")


def test_a_public_holiday_on_a_rest_day_does_not_count(
    employee, tenant, pay_period, rules, schedule
):
    PublicHoliday.objects.create(
        holiday_date=SUNDAY,  # Sunday is not a working day in this schedule
        name="Test holiday on a rest day",
        is_statutory=True,
        source_reference="Test fixture",
    )

    summary = recompute_summary(employee, pay_period)

    assert summary.total_public_holidays_not_worked == Decimal("0")


def test_a_public_holiday_actually_worked_does_not_double_count(
    employee, tenant, pay_period, rules, schedule
):
    PublicHoliday.objects.create(
        holiday_date=MONDAY,
        name="Test holiday worked",
        is_statutory=True,
        source_reference="Test fixture",
    )
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.PUBLIC_HOLIDAY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    summary = recompute_summary(employee, pay_period)

    assert summary.total_public_holiday_hours > 0
    assert summary.total_public_holidays_not_worked == Decimal("0")


# ------------------------------ a Sunday that is also a public holiday (D-280)


@pytest.fixture
def works_sundays(db, tenant, schedule):
    """A contract cleaner's week. Sunday is an ordinary working day, so a public
    holiday falling on it is one this employee is owed under s18."""
    with tenant_context(tenant.pk):
        day = schedule.days.get(cycle_day=6)
        day.is_working_day = True
        day.ordinary_hours = Decimal("8")
        day.save()
    return schedule


def test_a_worked_sunday_holiday_is_not_counted_as_a_holiday_not_worked(
    employee, tenant, pay_period, rules, works_sundays
):
    """THE OVERPAYMENT this fix closes.

    Since s2(1) adds a Monday without taking the Sunday away, 9 August 2026 is
    both a Sunday and a public holiday — and an employee who works it is
    naturally captured as SUNDAY. This count used to require
    ``day_type == PUBLIC_HOLIDAY``, so the date read as a holiday nobody
    worked: the employee would have been paid the s18(2)(a) unworked-holiday
    day on top of the Sunday premium, for a day they were at work.
    """
    PublicHoliday.objects.create(
        holiday_date=SUNDAY,
        name="Test holiday on a Sunday",
        is_statutory=True,
        source_reference="Test fixture",
    )
    capture(
        employee,
        work_date=SUNDAY,
        day_type=DayType.SUNDAY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    summary = recompute_summary(employee, pay_period)

    assert summary.total_public_holidays_not_worked == Decimal("0")


def test_the_same_sunday_holiday_left_uncaptured_still_counts(
    employee, tenant, pay_period, rules, works_sundays
):
    """Watched NOT firing. The employee who stayed home on that Sunday is still
    owed the day, so the count must not have been widened into silence."""
    PublicHoliday.objects.create(
        holiday_date=SUNDAY,
        name="Test holiday on a Sunday",
        is_statutory=True,
        source_reference="Test fixture",
    )

    summary = recompute_summary(employee, pay_period)

    assert summary.total_public_holidays_not_worked == Decimal("1")


def test_a_holiday_taken_as_leave_still_counts_as_not_worked(
    employee, tenant, pay_period, rules, schedule
):
    """``days_worked_equivalent`` is 1.000 for a leave day, which is why the
    count reads hours actually worked instead. A day of leave is not a day at
    work, and the holiday is still owed."""
    PublicHoliday.objects.create(
        holiday_date=MONDAY,
        name="Test holiday",
        is_statutory=True,
        source_reference="Test fixture",
    )
    capture(employee, work_date=MONDAY, day_type=DayType.ABSENT_PAID)

    summary = recompute_summary(employee, pay_period)

    assert summary.total_public_holidays_not_worked == Decimal("1")
