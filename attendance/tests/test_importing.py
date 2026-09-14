"""Bulk attendance import — D-155, D-156. Every guard here is proven to fail
before it is trusted, and every message is asserted on, not just the class of
exception raised.

Unlike the employee import, this one REPLACES days that already exist —
re-importing a corrected file is the ordinary case — so most of this file is
about the three-way split (locked / approved / captured) and about reverse
actually restoring what it replaced rather than merely deleting it.
"""

from __future__ import annotations

import datetime
import io
from decimal import Decimal

import pytest

from attendance.capture import capture
from attendance.importing import (
    EXPECTED_COLUMNS,
    ImportRefusedError,
    apply_batch,
    build_template_workbook,
    parse_workbook,
    preview_batch,
    reverse_batch,
)
from attendance.models import AttendanceDay, AttendanceImportBatch
from calculators.attendance import DayType
from core.managers import tenant_context
from core.models import Tenant
from employees.models import Employee, WorkSchedule, WorkScheduleDay
from employers.models import Employer, PayGroup, Workplace
from statutory.models import Sector, WorkingTimeRuleSet

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)
MONDAY = datetime.date(2026, 3, 2)
TUESDAY = datetime.date(2026, 3, 3)
Status = AttendanceImportBatch.Status


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Sparkle Cleaning")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(
            tenant=tenant, trading_name="Sparkle Cleaning", sector=sector
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
def employee(db, tenant, employer, pay_group):
    with tenant_context(tenant.pk):
        return Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number="9001015009086",
        )


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


@pytest.fixture
def batch(db, tenant, employer):
    with tenant_context(tenant.pk):
        return AttendanceImportBatch.objects.create(
            tenant=tenant, employer=employer, period_start=START, period_end=TUESDAY
        )


def row(employee_obj, work_date, **overrides) -> dict:
    values = {
        "employee_number": employee_obj.employee_number,
        "first_name": employee_obj.first_name,
        "last_name": employee_obj.last_name,
        "work_date": work_date,
        "day_type": DayType.ORDINARY,
        "time_in": datetime.time(8, 0),
        "time_out": datetime.time(17, 0),
        "unpaid_break_minutes": 60,
        "hours_worked": "",
        "workplace": "",
        "is_standby": "",
        "comment": "",
    }
    values.update(overrides)
    return values


def build_workbook(rows: list[dict]) -> io.BytesIO:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Attendance"
    sheet.append([column.header for column in EXPECTED_COLUMNS])
    for values in rows:
        sheet.append([values.get(column.key, "") for column in EXPECTED_COLUMNS])

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def parsed(rows: list[dict]):
    return parse_workbook(build_workbook(rows))


def day_count(tenant):
    with tenant_context(tenant.pk):
        return AttendanceDay.objects.count()


# --------------------------------------------------------------- happy path


def test_a_period_imports_matching_manual_capture_buckets(
    batch, tenant, employee, pay_group, rules, schedule
):
    rows = parsed([row(employee, MONDAY), row(employee, TUESDAY, time_out=datetime.time(18, 0))])

    result = apply_batch(batch, rows)

    assert result.accepted_count == 2
    assert result.created_count == 2
    assert result.rejected_count == 0

    manual = capture(
        employee,
        work_date=TUESDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(18, 0),
        unpaid_break_minutes=60,
    )
    with tenant_context(tenant.pk):
        imported = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
        imported_tuesday = AttendanceDay.objects.get(employee=employee, work_date=TUESDAY)
    assert imported.ordinary_hours == Decimal("8.000")
    assert imported.overtime_hours == Decimal("0.000")
    # The manual capture above overwrote Tuesday with identical inputs — the
    # two paths must agree bit for bit.
    assert imported_tuesday.ordinary_hours == manual.ordinary_hours
    assert imported_tuesday.overtime_hours == manual.overtime_hours
    assert imported_tuesday.night_hours == manual.night_hours


def test_a_row_with_a_bare_hours_worked_figure_buckets_like_a_time_span(
    batch, tenant, employee, pay_group, rules, schedule
):
    rows = parsed(
        [row(employee, MONDAY, time_in="", time_out="", unpaid_break_minutes="", hours_worked="8")]
    )

    result = apply_batch(batch, rows)

    assert result.accepted_count == 1
    with tenant_context(tenant.pk):
        day = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
    assert day.ordinary_hours == Decimal("8.000")
    assert day.time_in is None
    assert day.time_out is None


def test_the_template_columns_are_the_importers_expected_columns():
    """THE ANTI-DRIFT TEST."""
    workbook = build_template_workbook()
    headers = [cell.value for cell in workbook["Attendance"][1]]

    assert headers == [column.header for column in EXPECTED_COLUMNS]


def test_the_template_carries_no_bucket_column():
    """Asserts on the spec itself, so this fails the day somebody adds one."""
    forbidden = {
        "ordinary_hours",
        "overtime_hours",
        "sunday_hours",
        "public_holiday_hours",
        "night_hours",
    }
    keys = {column.key for column in EXPECTED_COLUMNS}
    assert not (keys & forbidden)


def test_a_preview_writes_nothing(batch, tenant, employee, pay_group, rules, schedule):
    before_count = day_count(tenant)
    rows = parsed([row(employee, MONDAY)])

    result = preview_batch(batch, rows)

    assert result.accepted_count == 1
    assert result.created_count == 1
    assert day_count(tenant) == before_count
    with tenant_context(tenant.pk):
        batch.refresh_from_db()
    assert batch.status == Status.PREVIEW
    assert batch.accepted_count == 1


# ------------------------------------------------------------------ refusals


def test_both_time_and_hours_worked_is_refused(batch, tenant, employee, pay_group, rules, schedule):
    rows = parsed([row(employee, MONDAY, hours_worked="8")])  # already has time_in/time_out

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    assert "not both" in result.issues[0].message


def test_neither_time_nor_hours_worked_is_refused(
    batch, tenant, employee, pay_group, rules, schedule
):
    rows = parsed(
        [row(employee, MONDAY, time_in="", time_out="", unpaid_break_minutes="", hours_worked="")]
    )

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    assert "hours worked" in result.issues[0].message.lower()


def test_an_unknown_employee_number_is_refused(batch, tenant, employee, pay_group, rules, schedule):
    rows = parsed([row(employee, MONDAY, employee_number="EMP9999")])

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    assert "EMP9999" in result.issues[0].message


def test_a_name_that_does_not_match_the_employee_number_is_refused(
    batch, tenant, employee, pay_group, rules, schedule
):
    rows = parsed([row(employee, MONDAY, first_name="SomeoneElse")])

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    assert "does not match" in result.issues[0].message


def test_a_locked_day_is_refused_by_name_in_preview(
    batch, tenant, employee, pay_group, rules, schedule
):
    with tenant_context(tenant.pk):
        AttendanceDay.objects.create(
            tenant=tenant,
            employee=employee,
            work_date=MONDAY,
            day_type=DayType.ORDINARY,
            status=AttendanceDay.Status.LOCKED,
            locked_by_payroll_run_id_ref=42,
            ordinary_hours=Decimal("8"),
        )
    rows = parsed([row(employee, MONDAY)])

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    assert "locked" in result.issues[0].message.lower()
    assert "will not be replaced" in result.issues[0].message

    with pytest.raises(ImportRefusedError):
        apply_batch(batch, rows)
    with tenant_context(tenant.pk):
        day = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
    assert day.status == AttendanceDay.Status.LOCKED
    assert day.ordinary_hours == Decimal("8")


def test_an_approved_day_is_refused_without_the_flag_and_replaced_with_it(
    batch, tenant, employee, pay_group, rules, schedule
):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    with tenant_context(tenant.pk):
        AttendanceDay.objects.filter(employee=employee, work_date=MONDAY).update(
            status=AttendanceDay.Status.APPROVED
        )

    rows = parsed([row(employee, MONDAY, time_out=datetime.time(18, 0))])

    refused = preview_batch(batch, rows)
    assert refused.blocking_count == 1
    assert "approved" in refused.issues[0].message.lower()

    with pytest.raises(ImportRefusedError):
        apply_batch(batch, rows)
    with tenant_context(tenant.pk):
        untouched = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
    assert untouched.ordinary_hours == Decimal("8.000")  # unchanged

    result = apply_batch(batch, rows, allow_replacing_approved=True)
    assert result.blocking_count == 0
    assert result.replaced_count == 1
    assert result.created_count == 0


def test_a_replaced_day_is_reported_as_replaced_not_created(
    batch, tenant, employee, pay_group, rules, schedule
):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )
    rows = parsed([row(employee, MONDAY, time_out=datetime.time(18, 0)), row(employee, TUESDAY)])

    result = apply_batch(batch, rows)

    assert result.replaced_count == 1
    assert result.created_count == 1
    assert result.accepted_count == 2


# --------------------------------------------------------------------- reverse


def test_reverse_restores_a_replaced_day_and_deletes_a_created_one(
    batch, tenant, employee, pay_group, rules, schedule
):
    capture(
        employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
        comment="original capture",
    )
    with tenant_context(tenant.pk):
        before = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
        before_ordinary = before.ordinary_hours
        before_comment = before.comment

    rows = parsed(
        [
            row(employee, MONDAY, time_out=datetime.time(19, 0), comment="corrected"),
            row(employee, TUESDAY),
        ]
    )
    apply_batch(batch, rows)

    with tenant_context(tenant.pk):
        replaced = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
    # 8:00-17:00 with a 60-minute break is 8 hours, entirely ordinary; the
    # corrected 8:00-19:00 shift is 10 hours, 8 ordinary (the schedule's own
    # cap) and 2 overtime — overtime is what actually moves.
    assert before_ordinary == Decimal("8.000")
    assert replaced.overtime_hours == Decimal("2.000")
    assert replaced.import_batch_id == batch.pk

    reverse_batch(batch)

    with tenant_context(tenant.pk):
        batch.refresh_from_db()
        assert batch.status == Status.REVERSED
        restored = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
        assert AttendanceDay.objects.filter(employee=employee, work_date=TUESDAY).exists() is False

    assert restored.ordinary_hours == before_ordinary
    assert restored.overtime_hours == Decimal("0.000")
    assert restored.comment == before_comment
    assert restored.import_batch_id is None


def test_reverse_leaves_days_outside_the_batch_untouched(
    batch, tenant, employer, employee, pay_group, rules, schedule
):
    with tenant_context(tenant.pk):
        other_employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Sipho",
            last_name="Dlamini",
            date_of_birth=datetime.date(1985, 5, 5),
            mobile_number="+27820000002",
            email="sipho@example.com",
            id_number="8505055009087",
        )
        other_schedule = WorkSchedule.objects.create(
            tenant=tenant,
            employee=other_employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=other_schedule,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
    capture(
        other_employee,
        work_date=MONDAY,
        day_type=DayType.ORDINARY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(17, 0),
        unpaid_break_minutes=60,
    )

    rows = parsed([row(employee, MONDAY)])
    apply_batch(batch, rows)
    reverse_batch(batch)

    with tenant_context(tenant.pk):
        untouched = AttendanceDay.objects.get(employee=other_employee, work_date=MONDAY)
    assert untouched.ordinary_hours == Decimal("8.000")


def test_a_workplace_named_in_the_row_is_resolved_and_used(
    batch, tenant, employer, employee, pay_group, rules, schedule
):
    with tenant_context(tenant.pk):
        site = Workplace.objects.create(tenant=tenant, employer=employer, name="Head Office")

    rows = parsed([row(employee, MONDAY, workplace="head office")])  # case-insensitive match
    result = apply_batch(batch, rows)

    assert result.accepted_count == 1
    with tenant_context(tenant.pk):
        day = AttendanceDay.objects.get(employee=employee, work_date=MONDAY)
    assert day.workplace_id == site.pk


def test_an_unknown_workplace_is_refused(batch, tenant, employee, pay_group, rules, schedule):
    rows = parsed([row(employee, MONDAY, workplace="Nonexistent Site")])

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    assert "Nonexistent Site" in result.issues[0].message
