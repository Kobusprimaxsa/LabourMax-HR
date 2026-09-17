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
the coverage instead of closing the gap. The invariant that is actually
true replaced the false one: the balance always equals the ledger sum
exactly, and every negative balance is fully attributable to identifiable
rows — proven here against ``leave/negative_balances.py``, the same query an
employer is shown.

**Both denominations, not just days (P6 chunk 4, task 5).** Every example
samples DAYS or HOURS; HOURS gives the employee an ``EmployeeLeaveEntitlement``
override with ``accrual_method=PER_HOURS_WORKED`` before any cycle is
generated. No conversion between the two is ever performed (D-164) — every
action posts and reads whichever column the cycle it touches carries.

**Three leave types, and the REAL ENGINE (P6 chunk 4b, D-190).** Every example
samples ANNUAL, SICK or FAMILY_RESPONSIBILITY, and the actions run along ONE
timeline — a cursor that only moves forward — so the sequence can reach what
SICK and FAMILY_RESPONSIBILITY actually bring:

- ``work`` captures real attendance through ``attendance.capture.capture``,
  which is what SICK's first-six-months ratio accrues from
- ``advance`` jumps months, across the six-month transition, s27(1)'s four
  months and the 36-month sick cycle boundary
- ``engine`` runs ``leave.accrual.accrue_employee`` itself as at the cursor —
  the ratio, the transition, cycle two's upfront grant, the eligibility-gated
  family responsibility grant — not a hand-posted stand-in for them

and the s22(4) election is sampled too. Beyond reconciliation, every row the
engine writes is checked against what its rule says it must be:

- a SICK or FAMILY_RESPONSIBILITY row is in DAYS — both entitlements are
  stated in days (s22(2) six weeks of working days, s27(2) days), so a row in
  an hours cycle is a day figure in the wrong column, which reconciliation
  alone can never see: the wrong figure still sums
- after SICK's six-month transition the cycle's NET accrual — every accrual
  less every reversal of one — is exactly E, or E plus what was drawn when
  s22(4) is not exercised (D-181, D-186): one entitlement per cycle
- cycle two onward's upfront sick grant is exactly E
- a family responsibility grant only happens once s27(1) is met, dated the
  first eligible day (D-189); an ineligible application is refused naming
  s27(1), and writes nothing

D-176 IS THE RULE FOR WHAT HAPPENS WHEN THIS FAILS: the invariant or the
code is fixed, and the strategy is never narrowed to stop producing the
sequence.

Decimal arithmetic throughout, matching every other ledger figure in this
codebase — a float here would be exactly the bug invariant 6 exists to
catch elsewhere.
"""

from __future__ import annotations

import datetime
import itertools
import os
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.engagements import engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeLeaveEntitlement, WorkSchedule, WorkScheduleDay
from employers.models import Employer, EmployerSetting
from leave.accrual import (
    FAMILY_RESPONSIBILITY_BASIS,
    SICK_TRANSITION_BASIS,
    SICK_TRANSITION_UNREDUCED_BASIS,
    SICK_UPFRONT_BASIS,
    accrue_employee,
)
from leave.applications import FamilyResponsibilityIneligibleError, submit_application
from leave.authorisation import approve, cancel
from leave.balances import balance_as_at
from leave.cycles import ensure_cycles
from leave.eligibility import family_responsibility_eligibility
from leave.forfeiture import ForfeitureRefusedError, capture_forfeiture
from leave.ledger import post_transaction, reverse_transaction
from leave.models import LeaveApplication, LeaveCycle, LeaveTransaction, LeaveType
from leave.negative_balances import negative_balances
from statutory.models import Sector

pytestmark = pytest.mark.django_db

ZERO = Decimal("0")
BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)  # a Sunday; the first Monday is 2026-03-02

TransactionType = LeaveTransaction.TransactionType

# 40 keeps CI's run short. Raise it locally to hunt, e.g.
# LEAVE_PROPERTY_EXAMPLES=400 pytest leave/tests/test_reconciliation_property.py
MAX_EXAMPLES = int(os.environ.get("LEAVE_PROPERTY_EXAMPLES", "40"))

_counter = itertools.count(1)


def _make_id(n: int) -> str:
    body = f"900101{9000 + (n % 999):04d}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def owner_user(db):
    return AppUser.objects.create_user(email="property-owner@example.com", password="x" * 16)


def _fresh_employee_with_two_cycles(leave_type, unit: str, *, reduce_by_taken: bool = True):
    """A brand-new tenant, employer, employee, schedule and engagement, with
    TWO of ``leave_type``'s own cycles already ensured — "accruals over
    multiple cycles" needs somewhere to land.

    ``unit="hours"`` gives the employee a ``PER_HOURS_WORKED``
    ``EmployeeLeaveEntitlement`` override for ``leave_type`` BEFORE any cycle
    is generated. Returns the cycles as generated; which unit they carry is
    for the test to assert, not this builder.
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
        if not reduce_by_taken:
            EmployerSetting.objects.create(
                tenant=tenant,
                employer=employer,
                setting_key="SICK_FIRST_CYCLE_REDUCTION",
                value_type=EmployerSetting.ValueType.BOOLEAN,
                value_boolean=False,
                set_by_employer=True,
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
        if unit == LeaveCycle.Unit.HOURS and leave_type.code == LeaveType.Code.ANNUAL:
            EmployeeLeaveEntitlement.objects.create(
                tenant=tenant,
                employee=employee,
                leave_type=leave_type,
                accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
                effective_from=START,
            )
        elif unit == LeaveCycle.Unit.HOURS:
            # D-192: the statute offers SICK and FAMILY_RESPONSIBILITY no method,
            # so the sampled agreement is REFUSED at capture — asserted, by its
            # message, not skipped. The example carries on as a days employee.
            from django.db import IntegrityError, transaction

            try:
                with transaction.atomic():
                    EmployeeLeaveEntitlement.objects.create(
                        tenant=tenant,
                        employee=employee,
                        leave_type=leave_type,
                        accrual_method=EmployeeLeaveEntitlement.AccrualMethod.PER_HOURS_WORKED,
                        effective_from=START,
                    )
            except IntegrityError as refused:
                assert f"{leave_type.code} leave cannot use the per_hours_worked" in str(refused)
            else:
                raise AssertionError(f"a per-hours {leave_type.code} agreement was accepted")

    # Exactly two cycles: a horizon 1.5x cycle_months out always falls inside
    # cycle 2's own span, whatever cycle_months is — read, never a literal.
    horizon = START + relativedelta(months=int(leave_type.cycle_months * 1.5))
    cycles = ensure_cycles(employee, leave_type, horizon=horizon)
    assert len(cycles) == 2, "Two cycles, up front, for 'accruals over multiple cycles'."
    return employee, cycles


def _expected_unit(leave_type, unit: str) -> str:
    """HOURS only where hours are a real basis: an agreed per-hours-worked
    ANNUAL method (BCEA s20(2)). SICK (s22(2)) and FAMILY_RESPONSIBILITY
    (s27(2)) are entitlements in days whatever an entitlement row says."""
    if leave_type.code == LeaveType.Code.ANNUAL:
        return unit
    return LeaveCycle.Unit.DAYS


# ------------------------------------------------------------- reconciliation


def _quantity(txn) -> Decimal:
    return txn.days if txn.days is not None else txn.hours


def _independent_ledger_sum(cycle: LeaveCycle) -> Decimal:
    """A from-scratch sum, deliberately NOT reusing
    ``leave/balances.py::_COLUMN_FOR_TYPE`` — an independent computation
    path is what makes this a real check rather than the cache checking
    itself."""
    return sum(
        (_quantity(txn) for txn in LeaveTransaction.objects.filter(leave_cycle_id=cycle.pk)), ZERO
    )


def _reconcile(employee, leave_type, cycle_start) -> LeaveCycle:
    """``balance_as_at`` is the system's own accessor — it recomputes when
    ITS OWN staleness detection (not this test) says to. Comparing its
    result against an independent ledger sum is precisely "the cache and a
    from-scratch rebuild agree".
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
        # D-176, corrected: a negative balance is asserted to be FOUND, by the
        # same read-only query an employer is shown, with the rows behind it.
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


def _reconcile_every_cycle(employee, leave_type):
    """Every cycle that exists, not only the two made up front — the cursor
    can carry applications and the engine past cycle two's end."""
    for cycle in LeaveCycle.objects.filter(employee=employee, leave_type=leave_type):
        _reconcile(employee, leave_type, cycle.cycle_start)


def _net_accrual(cycle) -> Decimal:
    """Every ACCRUAL row less every reversal of one. Computed here, not by the
    engine, so the transition invariant is not the engine checking itself."""
    total = ZERO
    for txn in LeaveTransaction.objects.filter(leave_cycle_id=cycle.pk):
        if txn.transaction_type == TransactionType.ACCRUAL:
            total += _quantity(txn)
        elif (
            txn.transaction_type == TransactionType.REVERSAL
            and txn.reverses_transaction.transaction_type == TransactionType.ACCRUAL
        ):
            total += _quantity(txn)
    return total


def _drawn_before(cycle, before) -> Decimal:
    """Positive: TAKEN rows dated before ``before``, net of their reversals."""
    total = ZERO
    for txn in LeaveTransaction.objects.filter(leave_cycle_id=cycle.pk):
        if txn.transaction_type == TransactionType.TAKEN and txn.transaction_date < before:
            total -= _quantity(txn)
        elif (
            txn.transaction_type == TransactionType.REVERSAL
            and txn.reverses_transaction.transaction_type == TransactionType.TAKEN
            and txn.reverses_transaction.transaction_date < before
        ):
            total -= _quantity(txn)
    return total


def _check_engine_row(employee, leave_type, txn, *, as_at, reduce_by_taken):
    """What the engine wrote, against what its rule says it must be."""
    if leave_type.code == LeaveType.Code.ANNUAL:
        return

    assert txn.days is not None and txn.hours is None, (
        f"{leave_type.code} is an entitlement in DAYS, but the engine wrote "
        f"days={txn.days} hours={txn.hours} ({txn.calculation_basis}) into a "
        f"{txn.leave_cycle.unit} cycle — a day figure in the hours column, which the "
        f"ledger sum cannot see"
    )
    cycle = txn.leave_cycle
    entitlement = cycle.entitlement_quantity
    # Coverage evidence, reported by --hypothesis-show-statistics: a green run
    # only means something for the rows the sequences actually reached.
    event(f"engine wrote {leave_type.code} {txn.calculation_basis}")
    if LeaveTransaction.objects.filter(
        leave_cycle=cycle,
        transaction_type=TransactionType.REVERSAL,
        reverses_transaction__transaction_type=TransactionType.ACCRUAL,
    ).exists():
        event(f"engine wrote {txn.calculation_basis} after an accrual was reversed")

    if txn.calculation_basis in (SICK_TRANSITION_BASIS, SICK_TRANSITION_UNREDUCED_BASIS):
        drawn = _drawn_before(cycle, txn.transaction_date)
        expected = entitlement if reduce_by_taken else entitlement + drawn
        assert _net_accrual(cycle) == expected, (
            f"after the six-month transition cycle one's net accrual must be one "
            f"entitlement, {entitlement}"
            f"{'' if reduce_by_taken else f' plus {drawn} drawn (s22(4) not exercised)'}"
            f" — got {_net_accrual(cycle)}"
        )
    elif txn.calculation_basis == SICK_UPFRONT_BASIS:
        assert txn.days == entitlement
    elif txn.calculation_basis == FAMILY_RESPONSIBILITY_BASIS:
        eligibility = family_responsibility_eligibility(employee, as_at)
        assert eligibility.is_eligible, eligibility.reasons
        assert txn.transaction_date == max(cycle.cycle_start, eligibility.eligible_from)
        assert txn.days == entitlement


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
_work = st.fixed_dictionaries(
    {"kind": st.just("work"), "days": st.integers(min_value=1, max_value=10)}
)
_advance = st.fixed_dictionaries(
    {"kind": st.just("advance"), "months": st.integers(min_value=1, max_value=40)}
)
_engine = st.fixed_dictionaries({"kind": st.just("engine")})

action_strategy = st.one_of(
    _accrue, _adjust, _apply, _cancel, _forfeit, _reverse, _work, _advance, _engine
)


def _capture_worked_days(employee, cursor: datetime.date, count: int) -> datetime.date:
    """``count`` weekdays worked in full from ``cursor``; returns the day after
    the last one. Through ``capture()``, the same path the grid and the
    importer use, so ``days_worked_equivalent`` is the real figure."""
    from attendance.capture import capture
    from calculators.attendance import DayType

    a_date = cursor
    captured = 0
    while captured < count:
        if a_date.weekday() < 5:
            capture(
                employee,
                work_date=a_date,
                day_type=DayType.ORDINARY,
                time_in=datetime.time(8, 0),
                time_out=datetime.time(17, 0),
                unpaid_break_minutes=60,
            )
            captured += 1
        a_date += datetime.timedelta(days=1)
    return a_date


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    actions=st.lists(action_strategy, min_size=3, max_size=16),
    unit=st.sampled_from([LeaveCycle.Unit.DAYS, LeaveCycle.Unit.HOURS]),
    leave_type_key=st.sampled_from(["ANNUAL", "SICK", "FAMILY_RESPONSIBILITY"]),
    reduce_by_taken=st.booleans(),
)
def test_balance_reconciles_to_the_ledger_across_any_valid_sequence(
    minimum_age,
    leave_rules,
    working_time_rules,
    sick_first_period,
    sick_certificate_threshold,
    evidence_types,
    annual_type,
    sick_type,
    family_type,
    owner_user,
    actions,
    unit,
    leave_type_key,
    reduce_by_taken,
):
    event(f"{leave_type_key} / {unit} / s22(4) {'exercised' if reduce_by_taken else 'not'}")
    leave_type = {
        "ANNUAL": annual_type,
        "SICK": sick_type,
        "FAMILY_RESPONSIBILITY": family_type,
    }[leave_type_key]

    employee, cycles = _fresh_employee_with_two_cycles(
        leave_type, unit, reduce_by_taken=reduce_by_taken
    )
    expected_unit = _expected_unit(leave_type, unit)
    for cycle in cycles:
        assert cycle.unit == expected_unit, (
            f"{leave_type.code} with a {unit} entitlement row must carry a {expected_unit} "
            f"cycle, got {cycle.unit}"
        )
    cursor = START + datetime.timedelta(days=1)  # the first Monday; only ever moves forward

    applications: list[dict] = []  # {"application": obj, "cancelled": bool}
    reversible: list[dict] = []  # {"txn": obj, "cycle_start": date, "reversed": bool}

    with tenant_context(employee.tenant_id):
        _reconcile_every_cycle(employee, leave_type)

        for action in actions:
            kind = action["kind"]

            if kind == "accrue":
                cycle = cycles[action["cycle_index"]]
                txn = post_transaction(
                    employee=employee,
                    leave_cycle=cycle,
                    leave_type=leave_type,
                    transaction_type=TransactionType.ACCRUAL,
                    quantity=action["quantity"],
                    unit=cycle.unit,
                    transaction_date=cycle.cycle_start,
                    calculation_basis="manual",
                )
                reversible.append({"txn": txn, "cycle_start": cycle.cycle_start, "reversed": False})

            elif kind == "adjust":
                cycle = cycles[action["cycle_index"]]
                current = _reconcile(employee, leave_type, cycle.cycle_start)
                if action["positive"]:
                    quantity = action["quantity"]
                else:
                    quantity = -min(action["quantity"], current.balance_quantity)
                if quantity == 0:
                    continue
                txn = post_transaction(
                    employee=employee,
                    leave_cycle=cycle,
                    leave_type=leave_type,
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
                start = cursor
                end = start + datetime.timedelta(days=length - 1)
                if leave_type.code == LeaveType.Code.FAMILY_RESPONSIBILITY:
                    eligibility = family_responsibility_eligibility(employee, start)
                    if not eligibility.is_eligible:
                        before_count = LeaveApplication.objects.filter(employee=employee).count()
                        with pytest.raises(FamilyResponsibilityIneligibleError) as refused:
                            submit_application(
                                employee, leave_type=leave_type, start_date=start, end_date=end
                            )
                        assert "s27(1)" in str(refused.value)
                        after_count = LeaveApplication.objects.filter(employee=employee).count()
                        assert after_count == before_count, "a refusal writes nothing"
                        cursor = end + datetime.timedelta(days=1)
                        continue
                application = submit_application(
                    employee,
                    leave_type=leave_type,
                    start_date=start,
                    end_date=end,
                    is_part_day=is_part_day,
                )
                approved = approve(application, decided_by=owner_user)
                applications.append({"application": approved, "cancelled": False})
                cursor = end + datetime.timedelta(days=8)  # clear of the next EXCLUDE

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
                delta = sum((_quantity(t) for t in taken), ZERO)
                before = _reconcile(employee, leave_type, cycle_start).balance_quantity
                cancel(application, cancelled_reason="property test cancellation")
                target["cancelled"] = True
                after = _reconcile(employee, leave_type, cycle_start).balance_quantity
                assert after == before - delta, (
                    "cancelling must return the balance to precisely what it was "
                    "before the taken transaction(s) it reverses"
                )

            elif kind == "forfeit":
                cycle = cycles[action["cycle_index"]]
                current = _reconcile(employee, leave_type, cycle.cycle_start)
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
                delta = _quantity(txn)
                before = _reconcile(employee, leave_type, target["cycle_start"]).balance_quantity
                # D-176, corrected in P6 chunk 4: reversing T is a mechanical
                # negation of T alone, even when a later row relied on T's
                # contribution. The negative balance that can follow is the
                # honest answer, asserted FOUND by ``_reconcile`` — never
                # skipped here, so the sequence stays in the generator.
                reverse_transaction(txn, reason="property test reversal")
                target["reversed"] = True
                after = _reconcile(employee, leave_type, target["cycle_start"]).balance_quantity
                assert after == before - delta, (
                    "a reversal must return the balance to precisely what it was "
                    "before the transaction it reverses"
                )

            elif kind == "work":
                cursor = _capture_worked_days(employee, cursor, action["days"])

            elif kind == "advance":
                cursor = cursor + relativedelta(months=action["months"])

            elif kind == "engine":
                txn = accrue_employee(employee, leave_type, as_at=cursor)
                if txn is not None:
                    _check_engine_row(
                        employee,
                        leave_type,
                        txn,
                        as_at=cursor,
                        reduce_by_taken=reduce_by_taken,
                    )
                    reversible.append(
                        {
                            "txn": txn,
                            "cycle_start": txn.leave_cycle.cycle_start,
                            "reversed": False,
                        }
                    )

            _reconcile_every_cycle(employee, leave_type)
