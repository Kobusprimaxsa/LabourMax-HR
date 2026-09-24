"""Property-based invariants over the payroll calculators.

P6's ``leave/tests/test_reconciliation_property.py`` is the model, and its
history is the argument: it found a day entitlement written into an hours cycle,
a reversed accrual still deducted from the six-month sick top-up, and an
unquantized ratio carrying 28 decimal digits. None was visible to an example.

**The rule from D-176, and it binds this file.** If a generated case fails, fix
the INVARIANT (when the property stated was false) or the CODE (when the code
was wrong). Never narrow a generator to make a failure go away — that deletes
the coverage that found it. Where a generator deliberately produces inputs a
calculator REFUSES, the property accepts the calculator's own refusal type and
nothing else: an arithmetic error escaping is a defect, a named refusal is the
guard working. ``hypothesis.event`` counts the refusals so a run that refused
everything is visible in ``--hypothesis-show-statistics``.

What these properties found is recorded against D-284.

Examples per property are configurable, the way LEAVE_PROPERTY_EXAMPLES is::

    PAYROLL_PROPERTY_EXAMPLES=2000 pytest calculators/tests/test_payroll_properties.py
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from calculators.attendance import AttendanceDayInput, DayType, bucket_day
from calculators.base import CENTS, EXACT, ZERO, CalculationTrace, Money, StatutoryFigure
from calculators.bonus import (
    BonusInput,
    BonusInputError,
    BonusRule,
    RateBasis,
    WeeklyWage,
    annual_bonus,
    cycle_containing,
)
from calculators.coida import CoidaEarning, CoidaInput, assessment_earnings
from calculators.gross import (
    DayPay,
    GrossInput,
    GrossPayRefusedError,
    NightAllowanceKind,
    PayBasis,
    PremiumRates,
    gross_pay,
)
from calculators.leave_pay import LeavePayInput, leave_pay
from calculators.paye import (
    PayeInput,
    PayeInputError,
    TaxBracket,
    TaxStatus,
    employees_tax,
)
from calculators.remuneration import AveragingWindow, RemunerationRefusedError
from calculators.sdl import SdlInput, levy
from calculators.termination import (
    NoticeBand,
    NoticeUnit,
    ProRataLeaveRule,
    SeveranceRule,
    TerminationInput,
    termination_payout,
)
from calculators.tests.test_gross import NINE_HOUR_RULES
from calculators.tests.test_paye_golden import (
    BRACKETS_2027,
    CREDIT_2027,
    PRIMARY_2027,
    SECONDARY_2027,
    TERTIARY_2027,
)
from calculators.uif import UifInput, contribution

# 40 keeps CI's run short. Raise it locally to hunt.
MAX_EXAMPLES = int(os.environ.get("PAYROLL_PROPERTY_EXAMPLES", "40"))
#: DERANDOMISED at the default count and random when hunting. This file lives
#: under calculators/, whose 100% BRANCH gate measures test code too, and a
#: random 40 examples can miss one side of a helper's branch on an unlucky run —
#: a coverage failure that is nobody's defect. Setting the variable to hunt
#: turns the randomness back on, which is where new cases come from.
PROPERTY = settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    derandomize="PAYROLL_PROPERTY_EXAMPLES" not in os.environ,
)

MARCH = datetime.date(2026, 3, 31)


def money(low="0", high="100000", places=2):
    return st.decimals(
        min_value=Decimal(low),
        max_value=Decimal(high),
        places=places,
        allow_nan=False,
        allow_infinity=False,
    )


def figure(value, row_id, table="statutory_parameter") -> StatutoryFigure:
    return StatutoryFigure(value=value, table=table, row_id=row_id)


# ------------------------------------------------------------ invariant 6


def all_money(value):
    """Every ``Money`` reachable from a result: fields, lines, nested results."""
    if isinstance(value, Money):
        yield value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from all_money(getattr(value, field.name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from all_money(item)


def assert_invariant_6(result) -> int:
    """The rounded figure IS the exact one rounded, and the exact one is held at
    six places — through every calculator, not only where payslip_line's CHECK
    already enforces it (D-230). Returns how many were checked, so a result
    carrying no Money at all cannot pass vacuously."""
    count = 0
    for amount in all_money(result):
        assert amount.exact == amount.exact.quantize(EXACT), amount
        assert amount.rounded == amount.exact.quantize(CENTS, rounding=ROUND_HALF_UP), amount
        count += 1
    assert count, f"{type(result).__name__} carries no Money"
    return count


def test_the_invariant_6_check_fails_on_a_money_rounded_by_hand():
    """PROVE EVERY GUARD FAILS: a Money whose rounded half was computed some
    other way — here truncated rather than rounded half up."""
    wrong = Money(exact=Decimal("10.005000"), rounded=Decimal("10.00"))
    with pytest.raises(AssertionError):
        assert_invariant_6((wrong,))


# ------------------------------------------------------ trace replay helper


class RowStore(dict):
    """What "open the row by its key" means inside a pure test: every
    statutory object a generated input carried, keyed by (table, row_id)."""

    def add(self, *objects):
        for obj in objects:
            if obj is not None:
                self[(obj.table, obj.row_id)] = obj
        return self

    def recorded(self, trace: CalculationTrace, table: str):
        return [self[key] for key in trace.statutory_rows if key[0] == table]


def assert_replays(trace: CalculationTrace, replayed: CalculationTrace):
    """The phase's own "Done when": reproducible from the trace ALONE."""
    assert replayed.outputs == trace.outputs
    assert tuple(replayed.statutory_rows) == tuple(trace.statutory_rows)


def D(text: str) -> Decimal | None:  # noqa: N802 - reads like the type it builds
    return None if text == "" else Decimal(text)


def B(text: str) -> bool:  # noqa: N802
    assert text in ("True", "False"), text
    return text == "True"


# =============================================================== UIF


UIF_ROWS = st.builds(
    lambda ceiling, employee, employer: (
        figure(ceiling, 901),
        figure(employee, 902),
        figure(employer, 903),
    ),
    ceiling=money("0.01", "60000"),
    employee=money("0", "5", places=4),
    employer=money("0", "5", places=4),
)


@st.composite
def uif_inputs(draw):
    ceiling, employee, employer = draw(UIF_ROWS)
    exempt = draw(st.booleans())
    return UifInput(
        calculated_for=MARCH,
        remuneration=draw(money()),
        # Deliberately allowed to exceed the remuneration: that is the capture
        # error the calculator warns about, and it must still not over-deduct.
        commission=draw(money("0", "20000")),
        excluded_remuneration=draw(money("0", "20000")),
        monthly_ceiling=ceiling,
        employee_rate_percent=employee,
        employer_rate_percent=employer,
        is_exempt=exempt,
        exemption_reason=draw(st.sampled_from(["", "s4(1)(a): under 24 hours a month"])),
    )


def replay_uif(trace: CalculationTrace, store: RowStore):
    inputs = trace.inputs

    unused = list(trace.statutory_rows)

    def keyed(name):
        # A real row says what it is by its parameter_code; here two rates can
        # carry the same value, so take the first recorded row not yet used
        # whose value matches — and fail if no recorded row carries it at all.
        value = Decimal(inputs[name])
        key = next((key for key in unused if store[key].value == value), None)
        assert key is not None, f"no recorded row carries {name}={value}"
        unused.remove(key)
        return store[key]

    return contribution(
        UifInput(
            calculated_for=trace.calculated_for,
            remuneration=Decimal(inputs["remuneration"]),
            commission=Decimal(inputs["commission"]),
            excluded_remuneration=Decimal(inputs["excluded_remuneration"]),
            monthly_ceiling=keyed("monthly_ceiling"),
            employee_rate_percent=keyed("employee_rate_percent"),
            employer_rate_percent=keyed("employer_rate_percent"),
            is_exempt=B(inputs["is_exempt"]),
            exemption_reason=inputs["exemption_reason"],
        )
    ).trace


@PROPERTY
@given(data=uif_inputs())
def test_uif_never_exceeds_the_ceiling_it_was_handed_on_either_side(data):
    result = contribution(data)
    ceiling = data.monthly_ceiling.value

    assert ZERO <= result.contribution_base.exact <= ceiling
    for side, rate in (
        (result.employee, data.employee_rate_percent.value),
        (result.employer, data.employer_rate_percent.value),
    ):
        assert ZERO <= side.exact
        assert side.exact <= Money.of(ceiling * rate / Decimal("100")).exact
    if data.is_exempt:
        assert result.employee.exact == result.employer.exact == ZERO
    assert_invariant_6(result)


@PROPERTY
@given(data=uif_inputs())
def test_a_uif_trace_reproduces_its_own_outputs(data):
    result = contribution(data)
    store = RowStore().add(
        data.monthly_ceiling, data.employee_rate_percent, data.employer_rate_percent
    )
    assert_replays(result.trace, replay_uif(result.trace, store))


# =============================================================== SDL


@st.composite
def sdl_inputs(draw):
    return SdlInput(
        calculated_for=MARCH,
        leviable_amount=draw(money("-5000", "2000000")),
        rate_percent=figure(draw(money("0.0001", "5", places=4)), 911),
        employer_is_liable=draw(st.booleans()),
        exemption_reason=draw(st.sampled_from(["", "s4(b)"])),
    )


@PROPERTY
@given(data=sdl_inputs())
def test_sdl_is_levied_only_on_a_liable_employer_and_then_at_exactly_the_rate(data):
    """The brief's statement — "zero exactly when the exemption boolean is set,
    never otherwise" — is FALSE, and the generator found why twice: a liable
    employer who paid nothing this month owes nothing (a negative leviable
    amount is treated as nil), and one cent at a 0.0001% rate is a levy that
    rounds to nil at six places. Both are the invariant being wrong, not the
    code (D-176's rule). The true one: an employer not liable is never levied,
    and a liable one is levied the rate on the leviable amount, exactly."""
    result = levy(data)

    if not data.employer_is_liable:
        assert result.levy.exact == ZERO
    else:
        leviable = max(data.leviable_amount, ZERO)
        assert (
            result.levy.exact == Money.of(leviable * data.rate_percent.value / Decimal("100")).exact
        )
    assert_invariant_6(result)


@PROPERTY
@given(data=sdl_inputs())
def test_an_sdl_trace_reproduces_its_own_outputs(data):
    result = levy(data)
    store = RowStore().add(data.rate_percent)
    (rate,) = store.recorded(result.trace, "statutory_parameter")
    inputs = result.trace.inputs
    replayed = levy(
        SdlInput(
            calculated_for=result.trace.calculated_for,
            leviable_amount=Decimal(inputs["leviable_amount"]),
            rate_percent=rate,
            employer_is_liable=B(inputs["employer_is_liable"]),
            exemption_reason=inputs["exemption_reason"],
        )
    )
    assert_replays(result.trace, replayed.trace)


# =============================================================== PAYE


@st.composite
def bracket_ladders(draw):
    """A random but CONSISTENT ladder: contiguous bands from zero, an open top,
    each band's base the tax on everything below it, rates rising and never
    over 100%. ``check_paye_brackets()`` would accept every one of these."""
    count = draw(st.integers(min_value=1, max_value=7))
    widths = draw(st.lists(money("1000", "500000", 0), min_size=count - 1, max_size=count - 1))
    rates = sorted(
        draw(st.lists(money("0", "100", 0), min_size=count, max_size=count)),
    )
    brackets, start, base = [], ZERO, ZERO
    for order in range(count):
        end = start + widths[order] if order < count - 1 else None
        brackets.append(
            TaxBracket(
                income_from=start,
                income_to=end,
                base_tax=base,
                marginal_rate_percent=rates[order],
                table="paye_tax_bracket",
                row_id=1000 + order,
            )
        )
        if end is not None:
            base += (end - start) * rates[order] / Decimal("100")
            start = end
    return tuple(brackets)


REBATE_SETS = st.sampled_from(
    [(PRIMARY_2027,), (PRIMARY_2027, SECONDARY_2027), (PRIMARY_2027, SECONDARY_2027, TERTIARY_2027)]
)
PERIODS = st.sampled_from([Decimal("12"), Decimal("26"), Decimal("52")])


@st.composite
def paye_inputs(draw, *, brackets=None, status=TaxStatus.STANDARD):
    periods_in_year = draw(PERIODS)
    return PayeInput(
        calculated_for=MARCH,
        remuneration=draw(money("0", "400000")),
        allowable_deductions=draw(money("0", "20000")),
        annual_payment=draw(st.one_of(st.just(ZERO), money("0", "500000"))),
        periods_in_year=periods_in_year,
        periods_worked=draw(
            st.decimals(
                min_value=Decimal("0.0001"),
                max_value=periods_in_year,
                places=4,
                allow_nan=False,
            )
        ),
        brackets=brackets if brackets is not None else draw(bracket_ladders()),
        rebates=draw(REBATE_SETS),
        medical_scheme_members=draw(st.integers(min_value=0, max_value=6)),
        medical_credit=draw(st.one_of(st.none(), st.just(CREDIT_2027))),
        tax_status=status,
    )


@PROPERTY
@given(data=paye_inputs(), extra=money("0.01", "50000"))
def test_paye_is_monotonic_in_taxable_income(data, extra):
    """Earning more never reduces the tax, on ANY consistent ladder — not only
    the 2027 one ``test_paye.py`` already walks — with rebates, credits,
    deductions, part periods and an annual payment all in play."""
    lower = employees_tax(data)
    higher = employees_tax(dataclasses.replace(data, remuneration=data.remuneration + extra))
    bigger_bonus = employees_tax(
        dataclasses.replace(data, annual_payment=data.annual_payment + extra)
    )

    assert higher.tax.exact >= lower.tax.exact
    assert bigger_bonus.tax.exact >= lower.tax.exact
    assert_invariant_6(lower)


@PROPERTY
@given(data=paye_inputs())
def test_paye_never_exceeds_the_taxable_income(data):
    result = employees_tax(data)
    taxable = max(data.remuneration - data.allowable_deductions, ZERO)

    assert ZERO <= result.tax_on_remuneration.exact <= Money.of(taxable).exact
    assert ZERO <= result.tax_on_annual_payment.exact <= Money.of(data.annual_payment).exact
    assert result.tax.exact <= Money.of(taxable + data.annual_payment).exact


def replay_paye(trace: CalculationTrace, store: RowStore):
    inputs = trace.inputs
    credits = store.recorded(trace, "medical_tax_credit_rate")
    return employees_tax(
        PayeInput(
            calculated_for=trace.calculated_for,
            remuneration=Decimal(inputs["remuneration"]),
            allowable_deductions=Decimal(inputs["allowable_deductions"]),
            annual_payment=Decimal(inputs["annual_payment"]),
            periods_in_year=Decimal(inputs["periods_in_year"]),
            periods_worked=Decimal(inputs["periods_worked"]),
            brackets=tuple(
                sorted(store.recorded(trace, "paye_tax_bracket"), key=lambda b: b.income_from)
            ),
            rebates=tuple(store.recorded(trace, "paye_rebate")),
            medical_scheme_members=int(inputs["medical_scheme_members"]),
            medical_credit=credits[0] if credits else None,
            tax_status=TaxStatus(inputs["tax_status"]),
            directive_number=inputs["directive_number"],
            directive_percentage=D(inputs["directive_percentage"]),
            directive_amount=D(inputs["directive_amount"]),
        )
    ).trace


@st.composite
def any_status_paye(draw):
    status = draw(st.sampled_from(list(TaxStatus)))
    data = draw(paye_inputs(status=status))
    return dataclasses.replace(
        data,
        directive_number="IT-0001" if status in (TaxStatus.DIRECTIVE_FIXED_PCT,) else "",
        directive_percentage=draw(money("0", "45"))
        if status is TaxStatus.DIRECTIVE_FIXED_PCT
        else None,
        directive_amount=draw(money("0", "20000"))
        if status is TaxStatus.DIRECTIVE_FIXED_AMOUNT
        else None,
    )


@PROPERTY
@given(data=any_status_paye())
def test_a_paye_trace_reproduces_its_own_outputs(data):
    result = employees_tax(data)
    store = RowStore().add(*data.brackets, *data.rebates, data.medical_credit)
    assert_replays(result.trace, replay_paye(result.trace, store))
    assert_invariant_6(result)


# ========================================================= gross pay

WORKABLE = [DayType.ORDINARY, DayType.REST_DAY, DayType.SUNDAY, DayType.PUBLIC_HOLIDAY]
OTHER = [DayType.ABSENT_UNPAID, DayType.ABSENT_PAID, DayType.NO_WORK_AVAILABLE, DayType.LEAVE]
TIMES = st.times().map(lambda t: t.replace(second=0, microsecond=0))


@st.composite
def day_pays(draw, when):
    day_type = draw(st.sampled_from(WORKABLE + OTHER))
    worked = day_type in WORKABLE and draw(st.booleans())
    day = AttendanceDayInput(
        work_date=when,
        day_type=day_type,
        time_in=draw(TIMES) if worked else None,
        time_out=draw(TIMES) if worked else None,
        unpaid_break_minutes=draw(st.sampled_from([0, 30, 60])) if worked else 0,
        is_standby=draw(st.booleans()) and draw(st.booleans()) and worked,
        is_ordinary_working_day=draw(st.booleans()),
        scheduled_ordinary_hours=draw(
            st.sampled_from([ZERO, Decimal("4"), Decimal("8"), Decimal("9")])
        ),
    )
    return DayPay(day=day, hours=bucket_day(day, NINE_HOUR_RULES))


NIGHT = st.sampled_from(
    [
        (NightAllowanceKind.BY_AGREEMENT, None),
        (NightAllowanceKind.TIME_OFF, None),
        (NightAllowanceKind.PERCENTAGE, Decimal("10.0000")),
        (NightAllowanceKind.FIXED_AMOUNT, Decimal("25.00")),
    ]
)


@st.composite
def gross_inputs(draw):
    kind, value = draw(NIGHT)
    rates = PremiumRates(
        overtime_multiplier=Decimal("1.500"),
        sunday_multiplier_ordinary=Decimal("1.500"),
        sunday_multiplier_non_ordinary=Decimal("2.000"),
        public_holiday_worked_multiplier=Decimal("2.000"),
        public_holiday_not_worked_paid=draw(st.booleans()),
        night_allowance_type=kind,
        night_allowance_value=value,
        table="working_time_rule_set",
        row_id=31,
    )
    count = draw(st.integers(min_value=0, max_value=10))
    days = tuple(draw(day_pays(MARCH - datetime.timedelta(days=n))) for n in range(count))
    hourly = draw(money("0", "200", places=6))
    return GrossInput(
        calculated_for=MARCH,
        pay_basis=draw(st.sampled_from(list(PayBasis))),
        days=days,
        rates=rates,
        hourly_rate=hourly,
        daily_rate=draw(money("0", "2000", places=6)),
        ordinary_shift_hours=draw(st.sampled_from([Decimal("8"), Decimal("9")])),
        period_rate=draw(money("0", "40000")),
        working_days_in_period=draw(st.sampled_from([ZERO, Decimal("5"), Decimal("21.67")])),
        above_bcea_earnings_threshold=draw(st.booleans()),
    )


def mostly(valid, anything):
    """Seven draws in eight from the valid range, one from a range that includes
    what the calculator refuses. Both stay covered: this is weighting, not
    narrowing — the refused inputs are still generated, and D-176's rule is
    about never removing them. Without it the refusals compound across a
    dozen optional rows and 86% of termination examples never price anything."""
    # Not st.one_of(valid × 7, anything): Hypothesis leans towards the SIMPLEST
    # branch, and st.none() is simpler than any row, so that spelling still
    # refused four examples in five. An integer draw leans towards zero instead.
    return st.integers(min_value=0, max_value=7).flatmap(
        lambda pick: anything if pick == 7 else valid
    )


def priced_or_refused(calculator, data, refusal):
    try:
        return calculator(data)
    except refusal as refused:
        # The refusal's opening words with the figures stripped, so the
        # statistics group by REASON and a generator refusing everything for
        # one reason shows up as one large line.
        reason = re.sub(r"[-\d.,]+", "#", str(refused))[:48]
        event(f"{calculator.__name__} refused: {reason}")
        return None


@PROPERTY
@given(data=gross_inputs())
def test_gross_is_never_negative_and_every_line_holds_invariant_6(data):
    result = priced_or_refused(gross_pay, data, GrossPayRefusedError)
    if result is None:
        return
    assert result.gross.exact >= ZERO
    assert all(line.amount.exact >= ZERO for line in result.lines)
    assert result.gross.exact == Money.of(sum(line.amount.exact for line in result.lines)).exact
    assert_invariant_6(result)


def days_from_trace(trace: CalculationTrace) -> tuple[DayPay, ...]:
    """The inverse of ``calculators.gross.day_as_text``: the priced facts of
    each day, read back. Nothing here reads attendance — only the trace."""
    from calculators.attendance import AttendanceDayResult

    days = []
    for key in sorted(k for k in trace.inputs if k.startswith("day_")):
        (when, day_type, ordinary_day, standby, scheduled, *buckets) = trace.inputs[key].split("|")
        ordinary, overtime, sunday, holiday, night, guaranteed, standby_worked, dwe = map(
            Decimal, buckets
        )
        days.append(
            DayPay(
                day=AttendanceDayInput(
                    work_date=datetime.date.fromisoformat(when),
                    day_type=day_type,
                    time_in=None,
                    time_out=None,
                    unpaid_break_minutes=0,
                    is_standby=B(standby),
                    is_ordinary_working_day=B(ordinary_day),
                    scheduled_ordinary_hours=Decimal(scheduled),
                ),
                hours=AttendanceDayResult(
                    ordinary_hours=ordinary,
                    overtime_hours=overtime,
                    sunday_hours=sunday,
                    public_holiday_hours=holiday,
                    night_hours=night,
                    paid_hours_guaranteed=guaranteed,
                    standby_hours_worked=standby_worked,
                    days_worked_equivalent=dwe,
                ),
            )
        )
    return tuple(days)


def replay_gross(trace: CalculationTrace, store: RowStore):
    inputs = trace.inputs
    (rates,) = store.recorded(trace, "working_time_rule_set")
    return gross_pay(
        GrossInput(
            calculated_for=trace.calculated_for,
            pay_basis=PayBasis(inputs["pay_basis"]),
            days=days_from_trace(trace),
            rates=rates,
            hourly_rate=Decimal(inputs["hourly_rate"]),
            daily_rate=Decimal(inputs["daily_rate"]),
            ordinary_shift_hours=Decimal(inputs["ordinary_shift_hours"]),
            period_rate=Decimal(inputs["period_rate"]),
            working_days_in_period=Decimal(inputs["working_days_in_period"]),
            above_bcea_earnings_threshold=B(inputs["above_bcea_earnings_threshold"]),
        )
    ).trace


@PROPERTY
@given(data=gross_inputs())
def test_a_gross_trace_reproduces_its_own_outputs(data):
    result = priced_or_refused(gross_pay, data, GrossPayRefusedError)
    if result is None:
        return
    assert_replays(result.trace, replay_gross(result.trace, RowStore().add(data.rates)))


# =========================================================== leave pay

WEEKS_13 = figure(Decimal("13.000000"), 41)


@st.composite
def windows(draw):
    return draw(
        mostly(
            st.builds(
                AveragingWindow,
                weeks=st.just(WEEKS_13),
                weeks_available=mostly(money("0.01", "13"), money("-1", "20")),
                remuneration=mostly(money("0", "200000"), money("-100", "200000")),
            ),
            st.none(),
        )
    )


@st.composite
def leave_pay_inputs(draw):
    in_days = draw(st.booleans())
    quantity = draw(mostly(money("0.001", "30", places=3), money("-1", "30", places=3)))
    return LeavePayInput(
        calculated_for=MARCH,
        leave_days=quantity if in_days else ZERO,
        leave_hours=ZERO if in_days else quantity,
        daily_rate=draw(mostly(money("1", "2000", places=6), money("0", "2000", places=6))),
        hourly_rate=draw(mostly(money("1", "250", places=6), money("0", "250", places=6))),
        remuneration_is_variable=draw(st.booleans()),
        window=draw(windows()),
        days_per_week=draw(st.sampled_from([ZERO, Decimal("5"), Decimal("5"), Decimal("6")])),
        hours_per_week=draw(st.sampled_from([ZERO, Decimal("40"), Decimal("45"), Decimal("45")])),
    )


def replay_leave_pay(trace: CalculationTrace, store: RowStore):
    inputs = trace.inputs
    recorded = store.recorded(trace, "statutory_parameter")
    # A conditional expression, not an if: which way it goes depends on what the
    # generator drew, and a branch in a TEST helper would make the calculators'
    # 100% branch gate hang on a random draw.
    window = (
        AveragingWindow(
            weeks=recorded[0],
            weeks_available=Decimal(inputs["window_weeks_available"]),
            remuneration=Decimal(inputs["window_remuneration"]),
        )
        if recorded
        else None
    )
    return leave_pay(
        LeavePayInput(
            calculated_for=trace.calculated_for,
            leave_days=Decimal(inputs["leave_days"]),
            leave_hours=Decimal(inputs["leave_hours"]),
            daily_rate=Decimal(inputs["daily_rate"]),
            hourly_rate=Decimal(inputs["hourly_rate"]),
            remuneration_is_variable=B(inputs["remuneration_is_variable"]),
            window=window,
            days_per_week=Decimal(inputs["days_per_week"]),
            hours_per_week=Decimal(inputs["hours_per_week"]),
        )
    ).trace


@PROPERTY
@given(data=leave_pay_inputs())
def test_leave_pay_is_never_negative_and_replays_from_its_trace(data):
    result = priced_or_refused(leave_pay, data, RemunerationRefusedError)
    if result is None:
        return
    assert result.amount.exact >= ZERO
    assert_invariant_6(result)
    store = RowStore().add(data.window.weeks if data.window is not None else None)
    assert_replays(result.trace, replay_leave_pay(result.trace, store))


# ======================================================== annual bonus

BONUS_RULE = st.builds(
    BonusRule,
    weeks=st.sampled_from([Decimal("4.333"), Decimal("4.330")]),
    payment_month=st.integers(min_value=1, max_value=12),
    pro_rata_on_termination=st.booleans(),
    min_service_months=st.sampled_from([0, 0, 0, 3]),
    table=st.just("termination_rule_set"),
    row_id=st.just(91),
)


@st.composite
def bonus_inputs(draw, *, terminating=None):
    rule = draw(BONUS_RULE)
    as_at = draw(st.dates(datetime.date(2025, 1, 1), datetime.date(2028, 12, 31)))
    cycle_start, cycle_end = cycle_containing(as_at, rule.payment_month)
    service_start = draw(st.dates(datetime.date(2020, 1, 1), as_at))
    leaving = draw(st.booleans()) if terminating is None else terminating
    # One wage from before the service started, and maybe an increase later —
    # a history with a gap in it is its own refusal, tested by example.
    raised_on = draw(st.one_of(st.none(), st.dates(service_start, cycle_end)))
    first, second = draw(money("1", "5000")), draw(money("1", "5000"))
    wages = (
        (WeeklyWage(service_start, None, first),)
        if raised_on is None or raised_on <= service_start
        else (
            WeeklyWage(service_start, raised_on, first),
            WeeklyWage(raised_on, None, max(first, second)),
        )
    )
    return BonusInput(
        calculated_for=as_at,
        rule=rule,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        service_start=service_start,
        service_end=as_at if leaving else None,
        as_at=as_at,
        wages=wages,
        rate_basis=draw(st.sampled_from(list(RateBasis))),
        part_first_month_counts=draw(st.booleans()),
        is_termination=leaving,
        qualifies=draw(mostly(st.just(True), st.just(False))),
        disqualified_because=draw(st.sampled_from(["", "casual, BCCCI clause 4.5(f)"])),
    )


def bonus_from_trace(inputs, rule) -> BonusInput | None:
    """The bonus input, read back out of a trace's own keys — the bonus
    calculator's, or the termination payout's ``bonus_``-prefixed copy."""

    def day(text):
        return None if text == "" else datetime.date.fromisoformat(text)

    wages = []
    for key in sorted(k for k in inputs if k.startswith("wage_")):
        start, end, weekly = inputs[key].split("|")
        wages.append(WeeklyWage(day(start), day(end), Decimal(weekly)))
    return BonusInput(
        calculated_for=day(inputs["as_at"]),
        rule=rule,
        cycle_start=day(inputs["cycle_start"]),
        cycle_end=day(inputs["cycle_end"]),
        service_start=day(inputs["service_start"]),
        service_end=day(inputs["service_end"]),
        as_at=day(inputs["as_at"]),
        wages=tuple(wages),
        rate_basis=RateBasis(inputs["rate_basis"]),
        part_first_month_counts=B(inputs["part_first_month_counts"]),
        is_termination=B(inputs["is_termination"]),
        qualifies=B(inputs["qualifies"]),
        disqualified_because=inputs["disqualified_because"],
    )


@PROPERTY
@given(data=bonus_inputs(), later=st.integers(min_value=1, max_value=400))
def test_the_bonus_never_exceeds_a_full_year_grows_with_time_and_replays(data, later):
    """Never more than the weeks at the highest wage, never negative, never
    more than twelve months; a later date in the same cycle never earns less;
    and the trace replays."""
    result = annual_bonus(data)
    top = max(wage.weekly for wage in data.wages)

    assert 0 <= result.full_months <= 12
    assert ZERO <= result.amount.exact <= Money.of(top * data.rule.weeks).exact
    assert_invariant_6(result)

    (rule_key,) = result.trace.statutory_rows
    assert rule_key == (data.rule.table, data.rule.row_id)
    replayed = annual_bonus(bonus_from_trace(result.trace.inputs, data.rule))
    assert_replays(result.trace, replayed.trace)

    after = data.as_at + datetime.timedelta(days=later)
    if data.service_end is None and after <= data.cycle_end:
        grown = annual_bonus(dataclasses.replace(data, as_at=after, calculated_for=after))
        assert grown.full_months >= result.full_months
        assert grown.amount.exact >= result.amount.exact


def test_the_bonus_refuses_by_name_and_never_with_an_arithmetic_error():
    with pytest.raises(BonusInputError, match="gives no annual bonus"):
        annual_bonus(
            BonusInput(
                calculated_for=MARCH,
                rule=BonusRule(ZERO, None, False, 0, "termination_rule_set", 93),
                cycle_start=datetime.date(2026, 1, 1),
                cycle_end=datetime.date(2026, 12, 31),
                service_start=datetime.date(2026, 1, 1),
                service_end=None,
                as_at=MARCH,
                wages=(),
            )
        )


# ========================================================= termination

FOUR_MONTHS = figure(Decimal("4.000000"), 51)


@st.composite
def termination_inputs(draw):
    in_days = draw(st.booleans())
    band = draw(
        mostly(
            st.builds(
                NoticeBand,
                notice_value=money("0", "4", places=2),
                notice_unit=st.sampled_from(list(NoticeUnit)),
                table=st.just("termination_notice_band"),
                row_id=st.just(61),
            ),
            st.none(),
        )
    )
    return TerminationInput(
        calculated_for=MARCH,
        weekly_rate=draw(mostly(money("1", "10000", places=6), money("0", "10000", places=6))),
        daily_rate=draw(mostly(money("1", "2000", places=6), money("0", "2000", places=6))),
        hourly_rate=draw(mostly(money("1", "250", places=6), money("0", "250", places=6))),
        days_per_week=draw(st.sampled_from([ZERO, Decimal("5"), Decimal("5"), Decimal("6")])),
        hours_per_week=draw(st.sampled_from([ZERO, Decimal("40"), Decimal("45"), Decimal("45")])),
        remuneration_is_variable=draw(st.booleans()),
        window=draw(windows()),
        notice_is_paid_in_lieu=draw(st.booleans()),
        notice_band=band,
        accommodation_offset=draw(st.one_of(st.just(ZERO), money("0", "5000"))),
        leave_due_days=draw(money("0", "30", places=3)) if in_days else ZERO,
        leave_due_hours=ZERO if in_days else draw(money("0", "200", places=3)),
        incomplete_cycle_days=draw(money("0", "15", places=3)) if in_days else ZERO,
        incomplete_cycle_hours=ZERO if in_days else draw(money("0", "100", places=3)),
        days_worked_in_incomplete_cycle=draw(money("0", "300", places=0)),
        pro_rata_rule=draw(
            mostly(
                st.builds(
                    ProRataLeaveRule,
                    days_worked_per_leave_day=mostly(money("1", "30"), money("0", "30")),
                    table=st.just("leave_rule_set"),
                    row_id=st.just(71),
                ),
                st.none(),
            )
        ),
        pro_rata_minimum_service_months=draw(mostly(st.just(FOUR_MONTHS), st.none())),
        months_of_service=draw(money("0", "240", places=2)),
        negative_leave_balance=draw(st.one_of(st.just(ZERO), money("0", "10", places=3))),
        severance_rule=draw(
            mostly(
                st.builds(
                    SeveranceRule,
                    weeks_per_completed_year=money("0", "2", places=2),
                    requires_operational_reason=st.just(True),
                    table=st.just("termination_rule_set"),
                    row_id=st.just(81),
                ),
                st.none(),
            )
        ),
        dismissed_for_operational_requirements=draw(st.booleans()),
        unreasonably_refused_alternative_employment=draw(st.booleans()),
        completed_years_of_service=draw(money("0", "30", places=0)),
        annual_bonus=draw(st.one_of(st.none(), bonus_inputs(terminating=True))),
    )


def replay_termination(trace: CalculationTrace, store: RowStore):
    inputs = trace.inputs

    def one(table):
        rows = store.recorded(trace, table)
        return rows[0] if rows else None

    rules = store.recorded(trace, "termination_rule_set")
    parameters = {row.row_id: row for row in store.recorded(trace, "statutory_parameter")}
    window = (
        AveragingWindow(
            weeks=parameters[WEEKS_13.row_id],
            weeks_available=Decimal(inputs["window_weeks_available"]),
            remuneration=Decimal(inputs["window_remuneration"]),
        )
        if WEEKS_13.row_id in parameters
        else None
    )
    return termination_payout(
        TerminationInput(
            calculated_for=trace.calculated_for,
            weekly_rate=Decimal(inputs["weekly_rate"]),
            daily_rate=Decimal(inputs["daily_rate"]),
            hourly_rate=Decimal(inputs["hourly_rate"]),
            days_per_week=Decimal(inputs["days_per_week"]),
            hours_per_week=Decimal(inputs["hours_per_week"]),
            remuneration_is_variable=B(inputs["remuneration_is_variable"]),
            window=window,
            notice_is_paid_in_lieu=B(inputs["notice_is_paid_in_lieu"]),
            notice_band=one("termination_notice_band"),
            accommodation_offset=Decimal(inputs["accommodation_offset"]),
            leave_due_days=Decimal(inputs["leave_due_days"]),
            leave_due_hours=Decimal(inputs["leave_due_hours"]),
            incomplete_cycle_days=Decimal(inputs["incomplete_cycle_days"]),
            incomplete_cycle_hours=Decimal(inputs["incomplete_cycle_hours"]),
            days_worked_in_incomplete_cycle=Decimal(inputs["days_worked_in_incomplete_cycle"]),
            pro_rata_rule=one("leave_rule_set"),
            pro_rata_minimum_service_months=parameters.get(FOUR_MONTHS.row_id),
            months_of_service=Decimal(inputs["months_of_service"]),
            negative_leave_balance=Decimal(inputs["negative_leave_balance"]),
            severance_rule=next((r for r in rules if isinstance(r, SeveranceRule)), None),
            dismissed_for_operational_requirements=B(
                inputs["dismissed_for_operational_requirements"]
            ),
            unreasonably_refused_alternative_employment=B(
                inputs["unreasonably_refused_alternative_employment"]
            ),
            completed_years_of_service=Decimal(inputs["completed_years_of_service"]),
            annual_bonus=(
                bonus_from_trace(
                    {
                        k.removeprefix("bonus_"): v
                        for k, v in inputs.items()
                        if k.startswith("bonus_")
                    },
                    next(r for r in rules if isinstance(r, BonusRule)),
                )
                if "bonus_as_at" in inputs
                else None
            ),
        )
    ).trace


@PROPERTY
@given(data=termination_inputs())
def test_a_termination_payout_is_never_negative_and_replays_from_its_trace(data):
    result = priced_or_refused(termination_payout, data, RemunerationRefusedError)
    if result is None:
        return
    assert result.total.exact >= ZERO
    assert all(line.amount.exact >= ZERO for line in result.lines)
    assert_invariant_6(result)
    store = RowStore().add(
        data.notice_band,
        data.window.weeks if data.window is not None else None,
        data.pro_rata_rule,
        data.pro_rata_minimum_service_months,
        data.severance_rule,
        None if data.annual_bonus is None else data.annual_bonus.rule,
    )
    assert_replays(result.trace, replay_termination(result.trace, store))


# ================================================================ COIDA


@st.composite
def coida_inputs(draw):
    lines = draw(
        st.lists(
            st.builds(
                CoidaEarning,
                source_code=st.sampled_from(["3601", "3605", "3607", "3901"]),
                amount=mostly(money("0", "120000"), money("-20000", "120000")),
                is_coida_base=st.booleans(),
            ),
            max_size=14,
        )
    )
    return CoidaInput(
        calculated_for=MARCH,
        assessment_period_start=datetime.date(2026, 3, 1),
        assessment_period_end=datetime.date(2027, 2, 28),
        annual_ceiling=figure(draw(money("1", "800000")), 921),
        earnings=tuple(lines),
    )


def replay_coida(trace: CalculationTrace, store: RowStore):
    inputs = trace.inputs
    (ceiling,) = store.recorded(trace, "statutory_parameter")
    lines = []
    for key in sorted(k for k in inputs if k.startswith("line_")):
        code, amount, flag = inputs[key].split("|")
        lines.append(CoidaEarning(code, Decimal(amount), B(flag)))
    return assessment_earnings(
        CoidaInput(
            calculated_for=trace.calculated_for,
            assessment_period_start=datetime.date.fromisoformat(inputs["assessment_period_start"]),
            assessment_period_end=datetime.date.fromisoformat(inputs["assessment_period_end"]),
            annual_ceiling=ceiling,
            earnings=tuple(lines),
        )
    ).trace


@PROPERTY
@given(data=coida_inputs())
def test_coida_declares_the_flagged_earnings_capped_once_and_replays(data):
    """Declared is never over the ceiling, never over the flagged earnings,
    never negative, and a line whose flag is off never moves it — whatever
    order, sign or mixture the lines arrive in."""
    result = assessment_earnings(data)
    flagged = sum((line.amount for line in data.earnings if line.is_coida_base), ZERO)

    assert (
        result.declared.exact == Money.of(min(max(flagged, ZERO), data.annual_ceiling.value)).exact
    )
    assert ZERO <= result.declared.exact <= data.annual_ceiling.value
    assert result.capped == (flagged > data.annual_ceiling.value)
    assert_invariant_6(result)
    assert_replays(result.trace, replay_coida(result.trace, RowStore().add(data.annual_ceiling)))


# ============================================ net of statutory deductions


UIF_2027 = (
    figure(Decimal("17712.000000"), 901),
    figure(Decimal("1.000000"), 902),
    figure(Decimal("1.000000"), 903),
)


@PROPERTY
@given(gross=money("0", "400000"), members=st.integers(min_value=0, max_value=6))
def test_paye_and_uif_together_never_exceed_the_gross_they_came_off(gross, members):
    """The half of "net never negative" that exists before assembly (chunk 8).
    Net is gross less every deduction on the payslip, and only the statutory
    two are computed today; on the loaded 2027 figures they never take more
    than the gross between them. The whole property — every recurring
    deduction and the s34 cap included — is chunk 8's, and is recorded there."""
    paye = employees_tax(
        PayeInput(
            calculated_for=MARCH,
            remuneration=gross,
            allowable_deductions=ZERO,
            annual_payment=ZERO,
            periods_in_year=Decimal("12"),
            periods_worked=Decimal("1"),
            brackets=BRACKETS_2027,
            rebates=(PRIMARY_2027,),
            medical_scheme_members=members,
            medical_credit=CREDIT_2027,
        )
    )
    ceiling, employee, employer = UIF_2027
    uif = contribution(
        UifInput(
            calculated_for=MARCH,
            remuneration=gross,
            commission=ZERO,
            excluded_remuneration=ZERO,
            monthly_ceiling=ceiling,
            employee_rate_percent=employee,
            employer_rate_percent=employer,
        )
    )

    assert gross - paye.tax.rounded - uif.employee.rounded >= ZERO


def test_the_refusal_helper_does_not_swallow_an_arithmetic_error():
    """PROVE EVERY GUARD FAILS: only the calculator's OWN refusal is accepted.
    A ZeroDivisionError escaping a calculator is a defect, and it propagates."""

    def broken(_data):
        raise ZeroDivisionError("a divisor nobody checked")

    with pytest.raises(ZeroDivisionError):
        priced_or_refused(broken, None, RemunerationRefusedError)


def test_paye_refusals_are_named_and_typed():
    """Anything the PAYE generators produce that the calculator cannot price
    must be a PayeInputError — here, the one input it documents refusing."""
    data = PayeInput(
        calculated_for=MARCH,
        remuneration=Decimal("1000"),
        allowable_deductions=ZERO,
        annual_payment=ZERO,
        periods_in_year=Decimal("12"),
        periods_worked=ZERO,
        brackets=BRACKETS_2027,
        rebates=(PRIMARY_2027,),
    )
    with pytest.raises(PayeInputError, match="Pay periods must be positive"):
        employees_tax(data)


# ================================ what the properties found, kept as examples
#
# Each of these was found by a property above failing on a generated case, and
# is pinned here as the smallest example of it, so the defect has a name that
# survives a change to a generator (D-284).


def a_worked_sunday() -> GrossInput:
    day = AttendanceDayInput(
        work_date=datetime.date(2026, 3, 1),
        day_type=DayType.SUNDAY,
        time_in=datetime.time(8, 0),
        time_out=datetime.time(12, 0),
        unpaid_break_minutes=0,
        is_standby=False,
        is_ordinary_working_day=False,
        scheduled_ordinary_hours=ZERO,
    )
    return GrossInput(
        calculated_for=MARCH,
        pay_basis=PayBasis.HOURLY,
        days=(DayPay(day=day, hours=bucket_day(day, NINE_HOUR_RULES)),),
        rates=PremiumRates(
            overtime_multiplier=Decimal("1.500"),
            sunday_multiplier_ordinary=Decimal("1.500"),
            sunday_multiplier_non_ordinary=Decimal("2.000"),
            public_holiday_worked_multiplier=Decimal("2.000"),
            public_holiday_not_worked_paid=True,
            night_allowance_type=NightAllowanceKind.BY_AGREEMENT,
            night_allowance_value=None,
            table="working_time_rule_set",
            row_id=31,
        ),
        hourly_rate=Decimal("45.00"),
        daily_rate=Decimal("405.00"),
        ordinary_shift_hours=Decimal("9"),
    )


def test_a_gross_trace_records_every_day_it_priced():
    """The trace used to carry ``days=1`` and nothing about the day, so R405 of
    Sunday pay could be reproduced only from attendance rows a reversed run may
    delete."""
    data = a_worked_sunday()
    result = gross_pay(data)

    assert result.trace.inputs["day_01"] == (
        "2026-03-01|sunday|False|False|0|0.000|0.000|4.000|0.000|0.000|0.000|0.000|0.444"
    )
    assert_replays(result.trace, replay_gross(result.trace, RowStore().add(data.rates)))


def test_a_gross_trace_without_its_days_does_not_replay():
    """PROVE EVERY GUARD FAILS: strip the days back out — the trace as it was
    before D-284 — and the replay no longer reproduces the gross."""
    data = a_worked_sunday()
    result = gross_pay(data)
    stripped = dataclasses.replace(
        result.trace,
        inputs={k: v for k, v in result.trace.inputs.items() if not k.startswith("day_")},
    )

    with pytest.raises(AssertionError):
        assert_replays(result.trace, replay_gross(stripped, RowStore().add(data.rates)))


SEVERANCE_RULE = SeveranceRule(
    weeks_per_completed_year=Decimal("1.00"),
    requires_operational_reason=True,
    table="termination_rule_set",
    row_id=81,
)


def a_leaver(**overrides) -> TerminationInput:
    values = {
        "calculated_for": MARCH,
        "weekly_rate": Decimal("3000.00"),
        "daily_rate": Decimal("600.00"),
        "hourly_rate": Decimal("66.666667"),
        "days_per_week": Decimal("5"),
        "hours_per_week": Decimal("45"),
        "pro_rata_minimum_service_months": FOUR_MONTHS,
    }
    values.update(overrides)
    return TerminationInput(**values)


@pytest.mark.parametrize(
    "why_nothing_was_paid",
    [
        {"completed_years_of_service": ZERO},
        {
            "completed_years_of_service": Decimal("3"),
            "unreasonably_refused_alternative_employment": True,
        },
    ],
    ids=["no-completed-year", "s41(4)-refusal"],
)
def test_a_severance_rule_read_and_zeroed_is_still_recorded(why_nothing_was_paid):
    """An operational dismissal READS the severance rule even when s41(2) or
    s41(4) then pays nothing — and the replay, handed a trace without it,
    refused for want of the rule."""
    data = a_leaver(
        severance_rule=SEVERANCE_RULE,
        dismissed_for_operational_requirements=True,
        **why_nothing_was_paid,
    )
    result = termination_payout(data)

    assert result.severance.exact == ZERO
    assert ("termination_rule_set", 81) in result.trace.statutory_rows


def test_a_variable_earners_payout_records_the_window_and_the_pattern():
    """The s35(4) path divides the window's remuneration by its weeks and then
    by the working pattern; none of the four reached the trace."""
    window = AveragingWindow(
        weeks=WEEKS_13, weeks_available=Decimal("13"), remuneration=Decimal("39000.00")
    )
    data = a_leaver(
        remuneration_is_variable=True,
        window=window,
        leave_due_days=Decimal("5"),
    )
    result = termination_payout(data)
    inputs = result.trace.inputs

    assert result.leave_due_pay.exact == Decimal("3000.000000"), "R39 000 / 13 / 5 × 5 days"
    assert (inputs["days_per_week"], inputs["hours_per_week"]) == ("5", "45")
    assert (inputs["window_weeks_available"], inputs["window_remuneration"]) == ("13", "39000.00")
    store = RowStore().add(window.weeks, FOUR_MONTHS)
    assert_replays(result.trace, replay_termination(result.trace, store))


def test_an_exemption_reason_is_recorded_as_an_input():
    """The employer's declared reason was recorded only as an OUTPUT, so a
    replay built from the inputs had nothing to hand back."""
    ceiling, employee, employer = UIF_2027
    result = contribution(
        UifInput(
            calculated_for=MARCH,
            remuneration=Decimal("800.00"),
            commission=ZERO,
            excluded_remuneration=ZERO,
            monthly_ceiling=ceiling,
            employee_rate_percent=employee,
            employer_rate_percent=employer,
            is_exempt=True,
            exemption_reason="s4(1)(a): under 24 hours a month",
        )
    )
    assert result.trace.inputs["exemption_reason"] == "s4(1)(a): under 24 hours a month"
