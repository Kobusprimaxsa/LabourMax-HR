"""The capture grid through a REAL request — the first time capture(), bulk_fill()
and approve() run through one (D-299). Every refusal is asserted on its words,
on the cell or message it lands on."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse

from attendance.grid import month_grid
from attendance.models import AttendanceDay
from core.managers import tenant_context
from core.tests.web_world import JUNE, a_world, client_for, grid_kwargs

pytestmark = pytest.mark.django_db

TUESDAY = datetime.date(2026, 6, 2)


@pytest.fixture
def world(db):
    return a_world("Household", staff=2)


@pytest.fixture
def owner(world):
    return client_for(world.user, world.tenant)


def cell_url(world, employee=None, day=2):
    return reverse(
        "attendance:cell",
        kwargs={
            **grid_kwargs(world),
            "employee_uid": (employee or world.employee).public_uid,
            "day": day,
        },
    )


def type_into(client, world, value, *, day=2, employee=None, **extra):
    return client.post(
        cell_url(world, employee, day), {"value": value, **extra}, HTTP_HX_REQUEST="true"
    )


def stored(world, day=TUESDAY, employee=None):
    with tenant_context(world.tenant.pk):
        return AttendanceDay.objects.filter(
            employee=employee or world.employee, work_date=day
        ).first()


# ------------------------------------------------------------------ the page


def test_the_month_renders_every_employee_and_day_with_proposals(world, owner):
    page = owner.get(reverse("attendance:grid", kwargs=grid_kwargs(world))).content.decode()

    for employee in world.employees:
        assert employee.last_name in page
    assert page.count('data-date="2026-06-') == 30 * 2
    assert 'placeholder="O"' in page, "a salaried weekday shows the schedule's proposal"
    assert "Absent — unpaid" in page and ">A<" in page, "every day type has a letter"


def test_a_proposal_is_never_a_saved_row(world, owner):
    owner.get(reverse("attendance:grid", kwargs=grid_kwargs(world)))
    with tenant_context(world.tenant.pk):
        assert not AttendanceDay.objects.exists()


def test_the_proposal_comes_from_the_group_on_screen_not_a_stale_cache(world):
    """D-298: the employee's current_pay_group cache was never refreshed here,
    so the old month_grid read the row as NOT salaried and proposed nothing."""
    with tenant_context(world.tenant.pk):
        assert world.employee.current_pay_group_id is None
        without = month_grid([world.employee], JUNE)
        with_group = month_grid([world.employee], JUNE, pay_group=world.group)
    assert all(c.prefill is None for c in without.rows[0].cells)
    assert with_group.rows[0].cells[1].prefill is not None


# --------------------------------------------------------------- one cell


def test_typing_hours_saves_the_cell_and_signals_the_panel(world, owner):
    response = type_into(owner, world, "9")

    assert response.status_code == 200
    assert response["HX-Trigger"] == "cellSaved"
    body = response.content.decode()
    assert "t-ordinary" in body and 'value="9"' in body and "failed" not in body
    day = stored(world)
    assert (day.day_type, day.source) == ("ordinary", "manual")
    assert day.ordinary_hours + day.overtime_hours == Decimal("9.000")


def test_o_alone_saves_the_schedules_own_times(world, owner):
    type_into(owner, world, "o")

    day = stored(world)
    assert (day.time_in, day.time_out, day.unpaid_break_minutes) == (
        datetime.time(8, 0),
        datetime.time(17, 0),
        60,
    )
    assert day.ordinary_hours == Decimal("8.000")


@pytest.mark.parametrize(
    ("typed", "said"),
    [
        ("Z", "is not a day type"),
        ("hello", "is not a code"),
        ("", "Nothing was saved"),
        ("L", "approved leave application"),
        ("R8", "Rest day takes no hours"),
        ("25", "no more than 24"),
    ],
)
def test_a_refused_code_is_shown_on_its_cell_and_saves_nothing(world, owner, typed, said):
    response = type_into(owner, world, typed)

    body = response.content.decode()
    assert "failed" in body and 'role="alert"' in body
    assert said in body
    assert "HX-Trigger" not in response
    assert stored(world) is None


def test_a_locked_day_is_refused_naming_the_run(world, owner):
    with transaction.atomic(), tenant_context(world.tenant.pk):
        AttendanceDay.objects.create(
            tenant=world.tenant,
            employee=world.employee,
            work_date=TUESDAY,
            day_type="ordinary",
            status=AttendanceDay.Status.LOCKED,
            locked_by_payroll_run_id_ref=77,
        )

    body = type_into(owner, world, "9").content.decode()

    assert "02 June 2026" in body and "payroll run 77" in body and "locked" in body


def test_an_approved_day_is_refused_and_can_be_replaced_on_purpose(world, owner):
    type_into(owner, world, "9")
    with tenant_context(world.tenant.pk):
        AttendanceDay.objects.filter(work_date=TUESDAY).update(status="approved")

    refused = type_into(owner, world, "10").content.decode()
    assert "was approved" in refused and "Replace and withdraw approval" in refused
    assert stored(world).status == "approved"

    type_into(owner, world, "10", replace="approved")
    day = stored(world)
    assert day.status == "captured"
    assert day.ordinary_hours + day.overtime_hours == Decimal("10.000")


def test_a_day_outside_the_month_or_an_employee_off_the_group_is_404(world, owner):
    assert type_into(owner, world, "9", day=31).status_code == 404
    stranger = a_world("Elsewhere").employee
    assert type_into(owner, world, "9", employee=stranger).status_code == 404


def test_the_request_body_cannot_name_the_employee(world, owner):
    """Only WHAT was typed comes from the form. An employee id smuggled into
    the body is ignored: the URL's employee is the one written."""
    other = world.employees[1]
    type_into(owner, world, "9", employee_uid=str(other.public_uid), employee=world.employee)

    assert stored(world) is not None
    assert stored(world, employee=other) is None


# ------------------------------------------------------------ bulk actions


def test_filling_the_month_from_the_schedule_fills_empty_working_days_only(world, owner):
    type_into(owner, world, "5")  # one day already captured, and different

    response = owner.post(
        reverse("attendance:bulk", kwargs=grid_kwargs(world)),
        {"who": "all", "span": "month", "value": "schedule"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 204 and response["HX-Refresh"] == "true"
    with tenant_context(world.tenant.pk):
        days = AttendanceDay.objects.filter(work_date__month=6)
        assert days.count() == 22 * 2, "June 2026 has 22 weekdays, two employees"
        assert not days.filter(work_date__week_day__in=(1, 7)).exists(), "no weekends"
    kept = stored(world)
    assert kept.ordinary_hours + kept.overtime_hours == Decimal("5.000"), "left as it was"


def test_filling_one_week_for_one_employee_with_a_letter(world, owner):
    owner.post(
        reverse("attendance:bulk", kwargs=grid_kwargs(world)),
        {"who": str(world.employee.public_uid), "span": "week:3", "value": "A"},
    )
    with tenant_context(world.tenant.pk):
        filled = AttendanceDay.objects.filter(employee=world.employee)
        assert filled.count() == 5 and set(filled.values_list("day_type", flat=True)) == {
            "absent_unpaid"
        }
        assert not AttendanceDay.objects.filter(employee=world.employees[1]).exists()


def test_bulk_fill_refuses_hours_because_hours_are_typed_per_day(world, owner):
    owner.post(
        reverse("attendance:bulk", kwargs=grid_kwargs(world)),
        {"who": "all", "span": "month", "value": "9"},
    )
    page = owner.get(reverse("attendance:grid", kwargs=grid_kwargs(world))).content.decode()
    assert "hours differ day to day" in page
    with tenant_context(world.tenant.pk):
        assert not AttendanceDay.objects.exists()


# ------------------------------------------------------ exceptions, approval


def test_a_blocking_exception_is_named_on_the_panel_and_refuses_approval(world, owner):
    # 8 scheduled, 5 overtime: over the 3-hour daily overtime cap AND the
    # 12-hour daily ceiling — two blocking exceptions from one cell.
    type_into(owner, world, "13")

    panel = owner.get(reverse("attendance:exceptions", kwargs=grid_kwargs(world))).content.decode()
    assert "<strong>2</strong> blocking" in panel
    assert "12.00-hour daily ceiling" in panel
    assert world.employee.last_name in panel

    owner.post(reverse("attendance:approve", kwargs=grid_kwargs(world)))
    page = owner.get(reverse("attendance:grid", kwargs=grid_kwargs(world))).content.decode()
    assert "2 blocking exception(s) stand over this span" in page
    assert stored(world).status == "captured"


def test_a_clean_month_approves(world, owner):
    type_into(owner, world, "8")

    owner.post(reverse("attendance:approve", kwargs=grid_kwargs(world)))

    assert stored(world).status == "approved"


def test_approving_with_nothing_captured_says_so(world, owner):
    owner.post(reverse("attendance:approve", kwargs=grid_kwargs(world)))
    page = owner.get(reverse("attendance:grid", kwargs=grid_kwargs(world))).content.decode()
    assert "Nothing captured is waiting for approval" in page


@pytest.mark.django_db(transaction=True)
def test_the_grid_shows_real_rows_through_a_real_request():
    """D-295 at the screen: with the pin expired before the view, RLS returned
    no employees and the grid rendered EMPTY, not an error. transaction=True so
    no test wrapper hides it."""
    world = a_world("Household")
    client = client_for(world.user, world.tenant)
    type_into(client, world, "9")

    page = client.get(reverse("attendance:grid", kwargs=grid_kwargs(world))).content.decode()

    assert world.employee.last_name in page
    assert 'value="9"' in page


def test_an_hourly_group_is_never_filled_from_the_schedule(db):
    """D-25 through the bulk action: an attendance-driven day is typed, not
    invented in one click."""
    hourly = a_world("Cleaners", basis="hourly")
    client = client_for(hourly.user, hourly.tenant)

    client.post(
        reverse("attendance:bulk", kwargs=grid_kwargs(hourly)),
        {"who": "all", "span": "month", "value": "schedule"},
    )

    page = client.get(reverse("attendance:grid", kwargs=grid_kwargs(hourly))).content.decode()
    assert "typed, not filled from the schedule" in page
    assert 'placeholder=""' in page, "and no proposals are drawn for it either"
    with tenant_context(hourly.tenant.pk):
        assert not AttendanceDay.objects.exists()
