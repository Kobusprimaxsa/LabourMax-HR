"""P5's "a month for twenty employees is captured in under ten minutes" — MEASURED,
not asserted (D-300).

Two numbers, and neither is a human trial:

* **keystrokes**, from a model of ``static/js/grid.js``'s own key handling —
  Enter commits a cell and advances, wrapping to the next row, so an empty
  cell costs one keystroke to pass and a typed one costs its characters plus
  one. The model is tied to the script by ``test_the_model_is_the_scripts``:
  if the keyboard changes, this fails before the numbers can go stale;
* **round trips**, by DRIVING a real twenty-employee month through the real
  views and counting and timing every request.

What neither proves is how fast a person types, reads a timesheet, or notices
a mistake. The ten-minute claim is Kobus's to time (docs/PHASES.md).

The drive is slow, so it runs only on request::

    MEASURE_CAPTURE=1 pytest attendance/tests/test_capture_measurement.py -s
"""

from __future__ import annotations

import datetime
import os
import pathlib
import time

import pytest
from django.db import transaction
from django.urls import reverse

from attendance.models import AttendanceDay
from core.managers import tenant_context
from core.tests.web_world import a_world, an_employee, client_for, grid_kwargs

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "static" / "js" / "grid.js"
EMPLOYEES = 20
JUNE_DAYS = 30
JUNE_WEEKDAYS = 22


def hourly_month_keystrokes(employees=EMPLOYEES, days=JUNE_DAYS, worked=JUNE_WEEKDAYS, typed="9"):
    """Every worked day typed (``9`` then Enter), every other day passed with
    one Enter; Enter wraps from a row's last day to the next row's first."""
    per_row = worked * (len(typed) + 1) + (days - worked) * 1
    return employees * per_row


def salaried_month_keystrokes(exceptions_per_employee=2, arrows_per_exception=4):
    """Fill the month from the schedule (Tab to the button, Enter), then fix the
    exceptions: arrow to the cell, type one letter, Enter. Then approve."""
    fill = 9 + 1  # Tabs from the top of the page to the Fill button, and Enter
    edits = EMPLOYEES * exceptions_per_employee * (arrows_per_exception + 1 + 1)
    approve = 2
    return fill + edits + approve


def test_the_model_is_the_scripts():
    """The keystroke model assumes exactly this keyboard. Change the keyboard
    and this fails, so the numbers in PHASES.md cannot quietly go stale."""
    script = SCRIPT.read_text(encoding="utf-8")
    for key in ('"ArrowRight"', '"ArrowLeft"', '"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"'):
        assert key in script, key
    assert "c < width - 1 ? r : r + 1" in script, "Enter wraps to the next row"
    assert "input.select()" in script, "typing replaces a cell's contents"


def test_the_numbers():
    assert hourly_month_keystrokes() == 20 * (22 * 2 + 8) == 1040
    assert salaried_month_keystrokes() == 10 + 20 * 2 * 6 + 2 == 252


@pytest.mark.skipif(not os.environ.get("MEASURE_CAPTURE"), reason="set MEASURE_CAPTURE=1")
@pytest.mark.django_db(transaction=True)
def test_drive_a_twenty_employee_hourly_month_through_the_real_views():
    world = a_world("Cleaners", basis="hourly", staff=0)
    world.employees = [an_employee(world) for _ in range(EMPLOYEES)]
    client = client_for(world.user, world.tenant)

    started = time.perf_counter()
    page = client.get(reverse("attendance:grid", kwargs=grid_kwargs(world)))
    first_load = time.perf_counter() - started
    assert page.status_code == 200

    saves, slowest = 0, 0.0
    started = time.perf_counter()
    for employee in world.employees:
        for day in range(1, JUNE_DAYS + 1):
            if datetime.date(2026, 6, day).weekday() >= 5:
                continue
            one = time.perf_counter()
            response = client.post(
                reverse(
                    "attendance:cell",
                    kwargs={**grid_kwargs(world), "employee_uid": employee.public_uid, "day": day},
                ),
                {"value": "9"},
                HTTP_HX_REQUEST="true",
            )
            slowest = max(slowest, time.perf_counter() - one)
            assert response["HX-Trigger"] == "cellSaved"
            saves += 1
    saving = time.perf_counter() - started

    started = time.perf_counter()
    client.get(reverse("attendance:exceptions", kwargs=grid_kwargs(world)))
    panel = time.perf_counter() - started

    started = time.perf_counter()
    client.get(reverse("attendance:grid", kwargs=grid_kwargs(world)))
    full_load = time.perf_counter() - started

    # atomic FIRST (D-92): this test runs without pytest's wrapping transaction.
    with transaction.atomic(), tenant_context(world.tenant.pk):
        assert AttendanceDay.objects.count() == EMPLOYEES * JUNE_WEEKDAYS == saves

    print(
        f"\nMEASURED: 1 page load ({first_load:.2f}s), {saves} cell saves "
        f"({saving:.1f}s server time, {saving / saves * 1000:.0f} ms average, "
        f"{slowest * 1000:.0f} ms slowest), exceptions panel {panel:.2f}s, "
        f"full grid reload {full_load:.2f}s; "
        f"keystrokes modelled: {hourly_month_keystrokes()}"
    )
