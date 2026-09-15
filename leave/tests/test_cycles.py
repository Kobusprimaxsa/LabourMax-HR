"""``leave_cycle`` — anchored to the current engagement (D-B), lazy, idempotent,
and never overlapping (task 2).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from core.managers import tenant_context, tenant_context_of
from employees.engagements import engage, terminate
from leave.cycles import current_cycle, ensure_cycles
from leave.models import LeaveCycle

pytestmark = pytest.mark.django_db

START = datetime.date(2026, 3, 1)


def test_ensure_cycles_creates_cycle_one_anchored_to_engagement_start(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    created = ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))

    assert len(created) == 1
    cycle = created[0]
    assert cycle.cycle_number == 1
    assert cycle.cycle_start == engagement.start_date
    assert cycle.cycle_end == datetime.date(2027, 3, 1)
    assert cycle.unit == LeaveCycle.Unit.DAYS
    assert cycle.entitlement_quantity == Decimal("15.000")


def test_ensure_cycles_is_idempotent(employee, engagement, leave_rules, annual_type, schedule_5day):
    first = ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))
    assert len(first) == 1

    second = ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))
    assert second == [], "Generating twice must create nothing the second time."

    with tenant_context_of(employee):
        assert LeaveCycle.objects.filter(employee=employee, leave_type=annual_type).count() == 1


def test_ensure_cycles_fills_the_gap_up_to_a_later_horizon(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))
    later = ensure_cycles(employee, annual_type, horizon=datetime.date(2028, 6, 1))

    assert [c.cycle_number for c in later] == [2, 3]
    with tenant_context_of(employee):
        numbers = sorted(
            LeaveCycle.objects.filter(employee=employee, leave_type=annual_type).values_list(
                "cycle_number", flat=True
            )
        )
    assert numbers == [1, 2, 3]


def test_a_rehire_starts_a_fresh_cycle_and_does_not_inherit_the_old_balance(
    employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))
    with tenant_context_of(employee):
        old_cycle = LeaveCycle.objects.get(employee=employee, engagement=engagement, cycle_number=1)
        old_cycle.accrued_quantity = Decimal("10.000")
        old_cycle.balance_quantity = Decimal("10.000")
        old_cycle.save(update_fields=["accrued_quantity", "balance_quantity", "updated_at"])

    terminate(engagement, termination_date=datetime.date(2026, 8, 31), reason_code="resigned")

    rehire_start = datetime.date(2027, 1, 4)
    new_engagement = engage(employee, start_date=rehire_start, job_title="Domestic worker")
    assert new_engagement.engagement_number == 2

    created = ensure_cycles(employee, annual_type, horizon=datetime.date(2027, 3, 1))

    assert len(created) == 1
    new_cycle = created[0]
    assert new_cycle.engagement_id == new_engagement.pk
    assert new_cycle.cycle_number == 1
    assert new_cycle.cycle_start == rehire_start
    assert new_cycle.carried_in_quantity == Decimal("0.000")
    assert new_cycle.accrued_quantity == Decimal("0.000")
    assert new_cycle.balance_quantity == Decimal("0.000")

    with tenant_context_of(employee):
        old_cycle.refresh_from_db()
    assert old_cycle.accrued_quantity == Decimal("10.000"), (
        "The old engagement's cycle is untouched."
    )


def test_ensure_cycles_stops_at_termination_date(
    employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    terminate(engagement, termination_date=datetime.date(2026, 5, 15), reason_code="resigned")

    created = ensure_cycles(employee, annual_type, horizon=datetime.date(2029, 1, 1))

    assert len(created) == 1
    assert created[0].cycle_number == 1


def test_terminating_closes_the_open_cycle_immediately_not_at_the_next_rehire(
    employee, engagement, minimum_age, leave_rules, annual_type, schedule_5day
):
    """Task 5: the event closes the cycle where it happens — ``terminate()``
    — not lazily, the next time somebody happens to call ``ensure_cycles``
    for a re-hire. D-132's own lesson: a boundary that moves when somebody
    happens to look is one that is wrong in between.
    """
    ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))
    with tenant_context_of(employee):
        cycle = LeaveCycle.objects.get(employee=employee, engagement=engagement, cycle_number=1)
    assert cycle.status == LeaveCycle.Status.OPEN

    termination_date = datetime.date(2026, 8, 31)
    terminate(engagement, termination_date=termination_date, reason_code="resigned")

    with tenant_context_of(employee):
        cycle.refresh_from_db()
    assert cycle.status == LeaveCycle.Status.CLOSED, (
        "Closed by terminate() itself — no re-hire, no second ensure_cycles call."
    )
    assert cycle.cycle_end == termination_date + datetime.timedelta(days=1)


def test_current_cycle_returns_none_before_generation(
    employee, engagement, leave_rules, annual_type
):
    assert current_cycle(employee, annual_type, datetime.date(2026, 4, 1)) is None


def test_current_cycle_finds_the_covering_cycle(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))

    found = current_cycle(employee, annual_type, datetime.date(2026, 6, 1))

    assert found is not None
    assert found.cycle_start <= datetime.date(2026, 6, 1) < found.cycle_end


def test_cycles_never_overlap_for_one_employee_and_type(
    employee, engagement, leave_rules, annual_type, schedule_5day
):
    """The D-131 lesson: the EXCLUDE constraint, proven to actually fire.

    ``ensure_cycles`` itself never produces an overlap, so this reaches under
    it — directly at the ORM — to prove the database-level guard is real and
    not merely implied by the service function's own care.
    """
    ensure_cycles(employee, annual_type, horizon=datetime.date(2026, 6, 1))

    with tenant_context(employee.tenant_id):
        overlapping = LeaveCycle(
            tenant_id=employee.tenant_id,
            employee=employee,
            engagement=engagement,
            leave_type=annual_type,
            cycle_number=99,
            cycle_start=datetime.date(2026, 6, 1),
            cycle_end=datetime.date(2027, 6, 1),
            unit=LeaveCycle.Unit.DAYS,
        )
        with pytest.raises(IntegrityError) as raised, transaction.atomic():
            overlapping.save()

    assert "leave_cycle_no_overlapping_periods" in str(raised.value)
