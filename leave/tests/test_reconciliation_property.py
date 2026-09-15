"""P6's own definition of done, PROVEN rather than asserted (task 5).

docs/PHASES.md: "every employee's balance reconciles to their ledger in
every scenario, with no drift." "Every scenario" is a property, tested as
one: Hypothesis generates random but valid sequences of accruals (over
multiple cycles), applications (approved and cancelled, full day and part
day), adjustments with reasons, forfeitures, and reversals of any of them,
and after EVERY step this asserts:

- the balance equals the sum of the ledger, exactly, for every cycle
- whenever a balance IS negative, ``leave/negative_balances.py`` reports
  that exact cycle, with that exact balance, and at least one transaction
  to show for it
- a reversal of any transaction returns the balance to precisely what it
  was before that transaction
- the cache and a from-scratch rebuild agree

**D-176, corrected (P6 chunk 4, task 1): "no balance is ever negative" was
never actually true, and constraining the generator to keep it true only
hid the sequence that disproves it.** Accrue, spend against the accrual,
then reverse the accrual is a real business sequence — an accrual posted
in error, consumed, then corrected — and the ledger is right to let the
reversal go through mechanically even though a later transaction already
relied on the reversed one's contribution still being there. The balance
that results is the mathematically honest answer: an account in debt. The
first fix suppressed exactly this sequence in the generator, which removed
the coverage instead of closing the gap — in production the sequence still
runs and the balance still goes negative, and nothing told anyone. This
version restores the generator (the ``reverse`` action no longer skips a
reversal that would take a balance below zero) and replaces the false
invariant with the one that is actually true: the balance always equals
the ledger sum exactly, and every negative balance is fully attributable to
identifiable rows — proven here by checking it against
``leave/negative_balances.py``, the same query an employer is shown.

**Both denominations, not just days (P6 chunk 4, task 5).** The whole
sequence above is generated twice — once against a DAYS-denominated cycle
(the ordinary case) and once against an HOURS-denominated one, produced by
giving the employee an ``EmployeeLeaveEntitlement`` override with
``accrual_method=PER_HOURS_WORKED`` before any cycle is generated, which is
the one thing that flips ``leave/cycles.py::unit_for_method()``'s answer.
No conversion between the two is ever performed — D-164 still holds — every
action posts and reads whichever physical column (``days`` or ``hours``)
the cycle it is touching actually carries, via ``cycle.unit`` itself, never
a hardcoded ``LeaveCycle.Unit.DAYS``. A test that converted between them
would quietly bless the exact thing D-164 forbids.

Decimal arithmetic throughout, matching every other ledger figure in this
codebase — a float here would be exactly the bug invariant 6 exists to
catch elsewhere.
"""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.engagements import engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeLeaveEntitlement, WorkSchedule, WorkScheduleDay
from employers.models import Employer
from leave.applications import submit_application
from leave.authorisation import approve, cancel
from leave.balances import balance_as_at
from leave.cycles import ensure_cycles
from leave.forfeiture import ForfeitureRefusedError, capture_forfeiture
from leave.ledger import post_transaction, reverse_transaction
from leave.models import LeaveCycle, LeaveTransaction
from leave.negative_balances import negative_balances
from statutory.models import Sector

pytestmark = pytest.mark.django_db

ZERO = Decimal("0")
BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)  # a Sunday; the first Monday is 2026-03-02

TransactionType = LeaveTransaction.TransactionType

_counter = itertools.count(1)


def _make_id(n: int) -> str:
    body = f"900101{9000 + (n % 999):04d}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def owner_user(db):
    return AppUser.objects.create_user(email="property-owner@example.com", password="x" * 16)


def _fresh_employee_with_two_cycles(annual_type, unit: str = LeaveCycle.Unit.DAYS):
    """A brand-new tenant, employer, employee, schedule and engagement, with
    TWO annual leave cycles already ensured — "accruals over multiple
    cycles" needs somewhere to land.

    ``unit="hours"`` gives the employee a ``PER_HOURS_WORKED``
    ``EmployeeLeaveEntitlement`` override BEFORE any cycle is generated —
    the one thing ``leave/cycles.py::unit_for_method()`` reads to decide a
    cycle's own denomination (task 5).
    """
    n = next(_counter)
    tenant = Tenant.objects.create(trading_name=f"Household {n}")
    with tenant_context(tenant.pk):
        sector = Sector.objects.filter(code=Sector.Code.DOMESTIC).first()
        if sector is None:
            sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
        employer = Employer.objects.create(
            tenant=tenant, trading_name=f"Household {n}", sector=sector
        )
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Property",
            last_name=f"Test{n}",
            date_of_birth=BORN,
            mobile_number=f"+2782{n:07d}",
            email=f"property{n}@example.com",
            id_number=_make_id(n),
        )
    engage(employee, start_date=START, job_title="Domestic worker")
    with tenant_context(tenant.pk):
        schedule = WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("40"),
            effective_from=START,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=schedule,
                cycle_day=cycle_day,
                is_working_day=cycle_day < 5,
                ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
            )
        if unit == LeaveCycle.Unit.HOURS:
            EmployeeLeaveEntitlement.objects.create(
                tenant=tenant,
                employee=employee,
                leave_type=annual_type,
                accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
                effective_from=START,
            )

    cycles = ensure_cycles(employee, annual_type, horizon=datetime.date(2027, 6, 1))
    assert len(cycles) == 2, "Two cycles, up front, for 'accruals over multiple cycles'."
    for cycle in cycles:
        assert cycle.unit == unit, f"expected a {unit}-denominated cycle, got {cycle.unit}"
    return employee, cycles


# ------------------------------------------------------------- reconciliation


def _independent_ledger_sum(cycle: LeaveCycle) -> Decimal:
    """A from-scratch sum, deliberately NOT reusing
    ``leave/balances.py::_COLUMN_FOR_TYPE`` — an independent computation
    path is what makes this a real check rather than the cache checking
    itself."""
    total = ZERO
    for txn in LeaveTransaction.objects.filter(leave_cycle_id=cycle.pk):
        value = txn.days if txn.days is not None else txn.hours
        total += value
    return total


def _reconcile(employee, leave_type, cycle_start) -> LeaveCycle:
    """``balance_as_at`` is the system's own accessor — it recomputes when
    ITS OWN staleness detection (not this test) says to. Comparing its
    result against an independent ledger sum is precisely "the cache and a
    from-scratch rebuild agree": if the staleness signal ever failed to
    fire, or the recompute logic mis-summed a column, this is where it
    would show up as a mismatch.
    """
    cycle = balance_as_at(employee, leave_type, cycle_start)
    assert cycle is not None
    ledger_sum = _independent_ledger_sum(cycle)
    assert cycle.balance_quantity == ledger_sum, (
        f"balance {cycle.balance_quantity} != ledger sum {ledger_sum} for cycle "
        f"{cycle.pk} ({cycle.cycle_start} - {cycle.cycle_end})"
    )
    assert isinstance(cycle.balance_quantity, Decimal)

    if cycle.balance_quantity < 0:
        # The replacement invariant (D-176, corrected): a negative balance is
        # never asserted away — it is asserted to be FOUND, by the same
        # read-only query an employer is shown, with the rows that caused it.
        reported = {n.cycle.pk: n for n in negative_balances(employee.employer)}
        found = reported.get(cycle.pk)
        assert found is not None, (
            f"cycle {cycle.pk} balance {cycle.balance_quantity} is negative but "
            f"leave.negative_balances.negative_balances() did not report it"
        )
        assert found.balance == cycle.balance_quantity
        assert found.causing_transactions, (
            "a reported negative balance must carry the transactions that caused it"
        )

    return cycle


# ------------------------------------------------------------------ actions


_accrue = st.fixed_dictionaries(
    {
        "kind": st.just("accrue"),
        "cycle_index": st.integers(min_value=0, max_value=1),
        "quantity": st.decimals(
            min_value="0.1", max_value="3.0", places=3, allow_nan=False, allow_infinity=False
        ),
    }
)
_adjust = st.fixed_dictionaries(
    {
        "kind": st.just("adjust"),
        "cycle_index": st.integers(min_value=0, max_value=1),
        "quantity": st.decimals(
            min_value="0.1", max_value="2.0", places=3, allow_nan=False, allow_infinity=False
        ),
        "positive": st.booleans(),
    }
)
_apply = st.fixed_dictionaries(
    {
        "kind": st.just("apply"),
        "length": st.integers(min_value=1, max_value=3),
        "part_day": st.booleans(),
    }
)
_cancel = st.fixed_dictionaries(
    {"kind": st.just("cancel"), "pick": st.integers(min_value=0, max_value=999)}
)
_forfeit = st.fixed_dictionaries(
    {
        "kind": st.just("forfeit"),
        "cycle_index": st.integers(min_value=0, max_value=1),
        "quantity": st.decimals(
            min_value="0.1", max_value="2.0", places=3, allow_nan=False, allow_infinity=False
        ),
    }
)
_reverse = st.fixed_dictionaries(
    {"kind": st.just("reverse"), "pick": st.integers(min_value=0, max_value=999)}
)

action_strategy = st.one_of(_accrue, _adjust, _apply, _cancel, _forfeit, _reverse)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    actions=st.lists(action_strategy, min_size=3, max_size=12),
    unit=st.sampled_from([LeaveCycle.Unit.DAYS, LeaveCycle.Unit.HOURS]),
)
def test_balance_reconciles_to_the_ledger_across_any_valid_sequence(
    minimum_age, leave_rules, working_time_rules, annual_type, owner_user, actions, unit
):
    employee, cycles = _fresh_employee_with_two_cycles(annual_type, unit)
    cursor_date = START + datetime.timedelta(days=1)  # the first Monday

    applications: list[dict] = []  # {"application": obj, "cancelled": bool}
    reversible: list[dict] = []  # {"txn": obj, "cycle_start": date, "reversed": bool}

    with tenant_context(employee.tenant_id):
        for cycle in cycles:
            _reconcile(employee, annual_type, cycle.cycle_start)

        for action in actions:
            kind = action["kind"]

            if kind == "accrue":
                cycle = cycles[action["cycle_index"]]
                txn = post_transaction(
                    employee=employee,
                    leave_cycle=cycle,
                    leave_type=annual_type,
                    transaction_type=TransactionType.ACCRUAL,
                    quantity=action["quantity"],
                    unit=cycle.unit,
                    transaction_date=cycle.cycle_start,
                    calculation_basis="manual",
                )
                reversible.append({"txn": txn, "cycle_start": cycle.cycle_start, "reversed": False})

            elif kind == "adjust":
                cycle = cycles[action["cycle_index"]]
                current = _reconcile(employee, annual_type, cycle.cycle_start)
                if action["positive"]:
                    quantity = action["quantity"]
                else:
                    quantity = -min(action["quantity"], current.balance_quantity)
                if quantity == 0:
                    continue
                txn = post_transaction(
                    employee=employee,
                    leave_cycle=cycle,
                    leave_type=annual_type,
                    transaction_type=TransactionType.ADJUSTMENT,
                    quantity=quantity,
                    unit=cycle.unit,
                    transaction_date=cycle.cycle_start,
                    calculation_basis="manual",
                    reason="property test adjustment",
                )
                reversible.append({"txn": txn, "cycle_start": cycle.cycle_start, "reversed": False})

            elif kind == "apply":
                length = action["length"]
                is_part_day = action["part_day"] and length == 1
                start = cursor_date
                end = start + datetime.timedelta(days=length - 1)
                application = submit_application(
                    employee,
                    leave_type=annual_type,
                    start_date=start,
                    end_date=end,
                    is_part_day=is_part_day,
                )
                approved = approve(application, decided_by=owner_user)
                applications.append({"application": approved, "cancelled": False})
                cursor_date = end + datetime.timedelta(days=8)  # clear of the next EXCLUDE

            elif kind == "cancel":
                pending = [a for a in applications if not a["cancelled"]]
                if not pending:
                    continue
                target = pending[action["pick"] % len(pending)]
                application = target["application"]
                cycle_start = application.start_date
                taken = list(
                    LeaveTransaction.objects.filter(
                        leave_application=application,
                        transaction_type=TransactionType.TAKEN,
                    )
                )
                delta = sum((t.days if t.days is not None else t.hours for t in taken), ZERO)
                before = _reconcile(employee, annual_type, cycle_start).balance_quantity
                cancel(application, cancelled_reason="property test cancellation")
                target["cancelled"] = True
                after = _reconcile(employee, annual_type, cycle_start).balance_quantity
                assert after == before - delta, (
                    "cancelling must return the balance to precisely what it was "
                    "before the taken transaction(s) it reverses"
                )

            elif kind == "forfeit":
                cycle = cycles[action["cycle_index"]]
                current = _reconcile(employee, annual_type, cycle.cycle_start)
                if current.balance_quantity <= 0:
                    continue
                quantity = min(action["quantity"], current.balance_quantity)
                if quantity <= 0:
                    continue
                try:
                    txn = capture_forfeiture(
                        cycle,
                        quantity=quantity,
                        reason="property test forfeiture",
                        captured_by=owner_user,
                    )
                except ForfeitureRefusedError:
                    continue
                reversible.append({"txn": txn, "cycle_start": cycle.cycle_start, "reversed": False})

            elif kind == "reverse":
                candidates = [r for r in reversible if not r["reversed"]]
                if not candidates:
                    continue
                target = candidates[action["pick"] % len(candidates)]
                txn = target["txn"]
                delta = txn.days if txn.days is not None else txn.hours
                before = _reconcile(employee, annual_type, target["cycle_start"]).balance_quantity
                # A REAL FINDING from this property test, recorded as D-176
                # and corrected in P6 chunk 4: reversing transaction T is a
                # purely mechanical negation of T alone — it does not know
                # that a LATER transaction (an adjustment or a forfeiture,
                # both capped against the balance AT THE TIME they were
                # posted) already relied on T's own contribution still being
                # there. Reversing T after that is legitimate bookkeeping —
                # the resulting negative balance is the mathematically
                # honest answer, an account now in debt. No longer skipped:
                # ``_reconcile`` now asserts the negative case is reported by
                # ``leave/negative_balances.py`` rather than asserting it
                # away, so this sequence stays IN the generator's coverage.
                reverse_transaction(txn, reason="property test reversal")
                target["reversed"] = True
                after = _reconcile(employee, annual_type, target["cycle_start"]).balance_quantity
                assert after == before - delta, (
                    "a reversal must return the balance to precisely what it was "
                    "before the transaction it reverses"
                )

            for cycle in cycles:
                _reconcile(employee, annual_type, cycle.cycle_start)
