"""Submitting a leave application — task 2, task 6.

Every guard here is proven by first watching the case it exists for: a
public holiday that must not be deducted, a rest day that must not be
deducted, a half day that deducts exactly half, an hourly employee whose
application deducts HOURS and never converts, an overdrawn application that
is approved rather than refused, and sick leave with no certificate that is
still TAKEN — never refused — and simply unpaid.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context_of
from leave.applications import ApplicationRefusedError, submit_application
from leave.cycles import ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveApplication, LeaveApplicationDay, LeaveCycle, LeaveTransaction

pytestmark = pytest.mark.django_db

TransactionType = LeaveTransaction.TransactionType

MONDAY = datetime.date(2026, 3, 2)
SUNDAY = datetime.date(2026, 3, 8)


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
    return cycle


def test_a_weeks_leave_over_a_public_holiday_deducts_four_days_not_five(
    employee, engagement, leave_rules, annual_type, schedule_5day, public_holiday_wednesday
):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=SUNDAY
    )

    assert application.total_days == Decimal("4.000"), (
        "Mon, Tue, Thu, Fri deduct; Wed (public holiday) and the weekend do not."
    )
    with tenant_context_of(employee):
        wednesday = LeaveApplicationDay.objects.get(
            leave_application=application, leave_date=datetime.date(2026, 3, 4)
        )
    assert wednesday.is_working_day is False
    assert wednesday.is_public_holiday is True
    assert wednesday.deducted_from_balance is False


def test_a_rest_day_inside_a_span_is_not_deducted(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=SUNDAY
    )

    assert application.total_days == Decimal("5.000"), "Mon-Fri deduct; Sat/Sun do not."
    with tenant_context_of(employee):
        saturday = LeaveApplicationDay.objects.get(
            leave_application=application, leave_date=datetime.date(2026, 3, 7)
        )
    assert saturday.is_working_day is False
    assert saturday.deducted_from_balance is False


def test_a_half_day_deducts_zero_point_five(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    application = submit_application(
        employee,
        leave_type=annual_type,
        start_date=MONDAY,
        end_date=MONDAY,
        is_part_day=True,
    )

    assert application.total_days == Decimal("0.500")
    with tenant_context_of(employee):
        day = LeaveApplicationDay.objects.get(leave_application=application, leave_date=MONDAY)
    assert day.day_portion == Decimal("0.500")


def test_a_multi_day_part_day_application_is_refused(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    with pytest.raises(ApplicationRefusedError) as raised:
        submit_application(
            employee,
            leave_type=annual_type,
            start_date=MONDAY,
            end_date=datetime.date(2026, 3, 3),
            is_part_day=True,
        )

    assert "exactly one date" in str(raised.value)


def test_an_hourly_employees_application_deducts_hours_never_days(
    employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    from employees.models import EmployeeLeaveEntitlement
    from leave.tests.conftest import START

    with tenant_context_of(employee):
        EmployeeLeaveEntitlement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            leave_type=annual_type,
            accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
            effective_from=START,
        )

    cycle = ensure_cycles(employee, annual_type, horizon=MONDAY)[0]
    assert cycle.unit == LeaveCycle.Unit.HOURS
    post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("20.000"),
        unit=LeaveCycle.Unit.HOURS,
        transaction_date=MONDAY,
        calculation_basis="manual",
    )

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=MONDAY
    )

    assert application.total_days == Decimal("0.000")
    assert application.total_hours is not None and application.total_hours > 0
    with tenant_context_of(employee):
        day = LeaveApplicationDay.objects.get(leave_application=application, leave_date=MONDAY)
    assert day.hours is not None and day.hours > 0


def test_an_overdrawn_application_is_created_with_exceeds_balance_and_unpaid_days(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    """AN OVERDRAWN APPLICATION IS NOT REFUSED (task 2). The excess falls to
    unpaid; it is never silently paid from a balance that is not there."""
    _grant_balance(employee, annual_type, quantity=Decimal("2.000"))

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=datetime.date(2026, 3, 6)
    )

    assert application.total_days == Decimal("5.000")
    assert application.exceeds_balance is True
    assert application.unpaid_days == Decimal("3.000")
    with tenant_context_of(employee):
        unpaid_count = LeaveApplicationDay.objects.filter(
            leave_application=application, is_paid=False
        ).count()
    assert unpaid_count == 3


# ------------------------------------------------------- unpaid_hours (D-188)


def _hours_basis(employee, annual_type):
    """A PER_HOURS_WORKED agreement on ANNUAL — the one thing that makes an
    application's unit HOURS (leave/cycles.py::unit_for_method)."""
    from employees.models import EmployeeLeaveEntitlement
    from leave.tests.conftest import START

    with tenant_context_of(employee):
        EmployeeLeaveEntitlement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            leave_type=annual_type,
            accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
            effective_from=START,
        )
    cycle = ensure_cycles(employee, annual_type, horizon=MONDAY)[0]
    assert cycle.unit == LeaveCycle.Unit.HOURS
    return cycle


def test_an_hourly_employees_overdraw_is_captured_in_unpaid_hours(
    employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    """Before unpaid_hours existed, an hours-basis overdraw had nowhere to be
    recorded: unpaid_days stayed 0 and the application read as fully paid
    although exceeds_balance was TRUE."""
    cycle = _hours_basis(employee, annual_type)
    post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("3.000"),
        unit=LeaveCycle.Unit.HOURS,
        transaction_date=MONDAY,
        calculation_basis="manual",
    )

    application = submit_application(
        employee, leave_type=annual_type, start_date=MONDAY, end_date=MONDAY
    )

    assert application.exceeds_balance is True
    assert application.total_hours == Decimal("8.000"), "the fixture schedule's Monday."
    # 8, not 5 (8 requested less 3 held): the overdraw falls to unpaid a WHOLE
    # DAY at a time, and approve() deducts only days marked deducted_from_balance
    # — so the whole Monday goes unpaid and the 3 hours stay in the balance.
    # unpaid_hours must agree with the day rows payroll will price, not with
    # the arithmetic shortfall. Whole-day granularity is recorded under D-188.
    assert application.unpaid_hours == Decimal("8.000")
    assert application.unpaid_days == Decimal("0"), "an hours application never carries days."
    with tenant_context_of(employee):
        stored = LeaveApplication.objects.get(pk=application.pk)
        unpaid_day_hours = sum(
            d.hours
            for d in LeaveApplicationDay.objects.filter(
                leave_application=application, is_working_day=True, is_paid=False
            )
        )
    assert stored.unpaid_hours == unpaid_day_hours == Decimal("8.000")


def test_leave_unpaid_by_nature_puts_every_hour_in_unpaid_hours(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    annual_type,
    annual_unauthorised_type,
    schedule_5day,
):
    """ANNUAL_UNAUTHORISED is unpaid by nature (is_paid=False). The balance
    COVERS it, so nothing is overdrawn — and every hour is still unpaid. The
    overdraw figure alone would have recorded zero."""
    cycle = _hours_basis(employee, annual_type)
    post_transaction(
        employee=employee,
        leave_cycle=cycle,
        leave_type=annual_type,
        transaction_type=TransactionType.ACCRUAL,
        quantity=Decimal("40.000"),
        unit=LeaveCycle.Unit.HOURS,
        transaction_date=MONDAY,
        calculation_basis="manual",
    )

    application = submit_application(
        employee,
        leave_type=annual_unauthorised_type,
        start_date=MONDAY,
        end_date=datetime.date(2026, 3, 3),
    )

    assert application.exceeds_balance is False
    assert application.total_hours == Decimal("16.000")
    assert application.unpaid_hours == Decimal("16.000")
    assert application.unpaid_days == Decimal("0")


def test_sick_leave_withheld_for_want_of_a_certificate_is_counted_as_unpaid(
    employee, engagement, leave_rules, sick_type, evidence_types, schedule_5day
):
    """Days basis: the unpaid portion lands in unpaid_days, in the
    application's own unit, and unpaid_hours stays zero — nothing in leave/
    converts a day into hours (D-164), and pricing a salaried day off the
    hourly rate is the very disagreement D-106 exists to prevent."""
    _grant_balance(employee, sick_type, quantity=Decimal("30.000"))

    application = submit_application(
        employee,
        leave_type=sick_type,
        start_date=MONDAY,
        end_date=datetime.date(2026, 3, 6),  # 5 working days, no evidence
    )

    assert application.exceeds_balance is False
    assert application.unpaid_days == Decimal("5.000")
    assert application.unpaid_hours == Decimal("0")


def _bare_application(employee, leave_type, **overrides):
    fields = {
        "tenant": employee.tenant,
        "employee": employee,
        "reference": "LV-TEST-1",
        "leave_type": leave_type,
        "start_date": MONDAY,
        "end_date": MONDAY,
    }
    fields.update(overrides)
    return LeaveApplication.objects.create(**fields)


def test_unpaid_hours_cannot_be_negative(employee, engagement, annual_type):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic(), tenant_context_of(employee):
            _bare_application(
                employee, annual_type, total_hours=Decimal("8"), unpaid_hours=Decimal("-1")
            )
    assert "leave_application_unpaid_hours_not_negative" in str(raised.value)


def test_unpaid_hours_cannot_be_null(employee, engagement, annual_type):
    """NOT NULL, so the >= 0 CHECK cannot be escaped by a NULL — which a CHECK
    alone would treat as satisfied (CLAUDE.md, nullable constraints)."""
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic(), tenant_context_of(employee):
            _bare_application(employee, annual_type, unpaid_hours=None)
    assert 'null value in column "unpaid_hours"' in str(raised.value)


def test_a_days_application_cannot_carry_unpaid_hours(employee, engagement, annual_type):
    """total_hours IS NULL says the application is days-basis. Hours on it
    would be a conversion somebody made — refused by the database."""
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic(), tenant_context_of(employee):
            _bare_application(employee, annual_type, unpaid_hours=Decimal("8"))
    assert "leave_application_unpaid_in_its_own_unit" in str(raised.value)


def test_an_hours_application_cannot_carry_unpaid_days(employee, engagement, annual_type):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic(), tenant_context_of(employee):
            _bare_application(
                employee, annual_type, total_hours=Decimal("8"), unpaid_days=Decimal("1")
            )
    assert "leave_application_unpaid_in_its_own_unit" in str(raised.value)


def test_sick_leave_with_no_certificate_beyond_the_threshold_is_taken_and_unpaid(
    employee, engagement, leave_rules, sick_type, evidence_types, schedule_5day
):
    """THE NEGATIVE HALF MATTERS: this must NOT be refused."""
    _grant_balance(employee, sick_type, quantity=Decimal("30.000"))

    application = submit_application(
        employee,
        leave_type=sick_type,
        start_date=MONDAY,
        end_date=datetime.date(2026, 3, 6),  # 5 working days, no evidence attached
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


def test_sick_leave_within_the_threshold_with_no_certificate_is_paid(
    employee, engagement, leave_rules, sick_type, evidence_types, schedule_5day
):
    _grant_balance(employee, sick_type, quantity=Decimal("30.000"))

    application = submit_application(
        employee,
        leave_type=sick_type,
        start_date=MONDAY,
        end_date=datetime.date(2026, 3, 3),  # 2 working days — within the threshold
    )

    with tenant_context_of(employee):
        paid_flags = set(
            LeaveApplicationDay.objects.filter(
                leave_application=application, is_working_day=True
            ).values_list("is_paid", flat=True)
        )
    assert paid_flags == {True}


def test_sick_leave_with_a_doctors_note_is_paid_regardless_of_length(
    employee, engagement, leave_rules, sick_type, evidence_types, schedule_5day
):
    _grant_balance(employee, sick_type, quantity=Decimal("30.000"))
    doctor_note = evidence_types["DOCTOR_NOTE"]

    application = submit_application(
        employee,
        leave_type=sick_type,
        start_date=MONDAY,
        end_date=datetime.date(2026, 3, 6),  # 5 working days, well beyond the threshold
        leave_evidence_type=doctor_note,
    )

    with tenant_context_of(employee):
        paid_flags = set(
            LeaveApplicationDay.objects.filter(
                leave_application=application, is_working_day=True
            ).values_list("is_paid", flat=True)
        )
    assert paid_flags == {True}


def test_an_application_ending_before_it_starts_is_refused(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    with pytest.raises(ApplicationRefusedError):
        submit_application(
            employee,
            leave_type=annual_type,
            start_date=MONDAY,
            end_date=datetime.date(2026, 3, 1),
        )


def test_references_are_unique_and_sequential_per_tenant(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    _grant_balance(employee, annual_type, quantity=Decimal("15.000"))

    first = submit_application(employee, leave_type=annual_type, start_date=MONDAY, end_date=MONDAY)
    second = submit_application(
        employee,
        leave_type=annual_type,
        start_date=datetime.date(2026, 3, 3),
        end_date=datetime.date(2026, 3, 3),
    )

    assert first.reference != second.reference
    assert first.reference.startswith("LV-2026-")
