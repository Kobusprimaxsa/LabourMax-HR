"""Leave pay — BCEA s21, calculated in accordance with s35.

Pure. The quantity of leave arrives already decided by ``leave/`` (which counts
days and hours and never money, D-166); this module only prices it.

**s21(1)**: "An employer must pay an employee leave pay at least equivalent to
the remuneration that the employee would have received for working for a period
equal to the period of annual leave, calculated — (a) at the employee's rate of
remuneration immediately before the beginning of the period of annual leave; and
(b) in accordance with section 35."

So there are two rates an employee's leave can be priced at, and s35 decides
which:

* **The ordinary rate**, for an employee paid by time whose pay does not swing.
  ``employee_remuneration``'s own derived daily and hourly rates (D-106), as at
  the day before the leave began.
* **The 13-week average**, where **s35(4)** bites: "If an employee's remuneration
  or wage is calculated, either wholly or in part, on a basis other than time or
  if an employee's remuneration or wage fluctuates significantly from period to
  period, any payment to that employee in terms of this Act must be calculated by
  reference to the employee's remuneration or wage during — (a) the preceding 13
  weeks; or (b) if the employee has been in employment for a shorter period, that
  period."

**Which of the two applies is DECLARED, not derived** (D-110's shape). "On a
basis other than time" is a fact about the contract and "fluctuates
significantly" has no statutory threshold at all — no figure anywhere says what
significant means, so nothing here may invent one. The employer states it; the
statute keeps the window; and the window itself is
``VARIABLE_EARNINGS_AVERAGE_WEEKS`` in ``statutory_parameter``, never a 13
written here.

**The average is NOT a maximum of the two.** s21(1)'s "at least equivalent to"
sets a floor on what the employer must pay, and (a) and (b) together say how the
figure is arrived at — (b) governs how (a)'s rate is determined. Paying the
greater of the flat rate and the average would be inventing an entitlement the
Act does not give. Where the average comes out BELOW the contractual rate the
result says so in a warning, because that is a payslip a human should look at,
but the figure is the figure.

**Weekly is the hub, here as everywhere** (D-106). The average is a weekly
figure — total remuneration over the window, divided by the window — and the
daily and hourly rates come off it by the employee's own days and hours per
week. Deriving daily as hourly × hours-per-day disagrees with weekly ÷
days-per-week whenever the two do not reconcile, and the employee would be paid
one figure for a day of leave and another for a day of work.

**A balance is priced in the unit it was accrued in** (D-164). Days at a daily
rate, hours at an hourly one, and this module converts between them exactly
never. Exactly one of the two quantities may be non-zero; both, or neither, is
refused.

**What counts as remuneration for the average is not decided here.** s35(5)
includes "the cash value of any payment in kind ... unless the employee receives
that payment in kind" and excludes "(i) gratuities; (ii) allowances paid to an
employee for the purposes of enabling an employee to work; and (iii) any
discretionary payments not related to the employee's hours of work or work
performance"; the Minister's determination in Government Notice 691 of 23 May
2003 lists both sides in detail. In this codebase that is the
``payroll_component.affects_leave_pay_average`` flag, decided once per component
with its reasoning, and the caller hands in the already-filtered total. A
calculator that re-decided it would be a second answer to a settled question.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from calculators.base import (
    ZERO,
    CalculationTrace,
    Money,
    PayslipLine,
    StatutoryFigure,
    as_text,
    rows_of,
)

CALCULATOR = "leave_pay.leave_pay"

COMPONENT = "LEAVE_PAY"


class LeavePayRefusedError(ValueError):
    """The leave cannot be priced, and guessing would be worse."""


@dataclasses.dataclass(frozen=True)
class AveragingWindow:
    """s35(4)'s window, and how much of it this employee actually has.

    ``weeks`` is the statutory figure — 13 — carrying the key of the row it came
    from. ``weeks_available`` is s35(4)(b): an employee in employment for a
    shorter period is averaged over that shorter period instead, so it is the
    lesser of the two and the caller works it out from the engagement.
    """

    weeks: StatutoryFigure
    weeks_available: Decimal
    #: Total s35(5) remuneration over ``weeks_available``, already filtered to
    #: the components whose ``affects_leave_pay_average`` is true.
    remuneration: Decimal

    @property
    def table(self) -> str:
        return self.weeks.table

    @property
    def row_id(self) -> int:
        return self.weeks.row_id


@dataclasses.dataclass(frozen=True)
class LeavePayInput:
    calculated_for: datetime.date
    #: Exactly one of these is non-zero (D-164). The leave ledger posts in the
    #: unit its own accrual produced and nothing converts between them.
    leave_days: Decimal
    leave_hours: Decimal
    #: The employee's own derived rates as at the day before the leave began —
    #: s21(1)(a)'s "rate of remuneration immediately before the beginning".
    daily_rate: Decimal
    hourly_rate: Decimal
    #: s35(4)'s trigger, declared: "on a basis other than time" is a fact about
    #: the contract, and "fluctuates significantly" has no statutory threshold.
    remuneration_is_variable: bool = False
    window: AveragingWindow | None = None
    #: The employee's own working pattern, for turning a weekly average into a
    #: daily or hourly one. Weekly is the hub (D-106).
    days_per_week: Decimal = ZERO
    hours_per_week: Decimal = ZERO


@dataclasses.dataclass(frozen=True)
class LeavePayResult:
    line: PayslipLine
    amount: Money
    #: The rate the leave was actually priced at, per day or per hour.
    rate_used: Money
    #: The s35(4) average, where one was used. None on the ordinary path.
    average_weekly: Money | None
    used_the_average: bool
    trace: CalculationTrace


def _refuse_an_unpriceable_quantity(data: LeavePayInput) -> Decimal:
    """Exactly one unit, and it must be positive. Returns nothing useful — the
    caller reads the two fields — but refuses everything that is not one."""
    if data.leave_days < ZERO or data.leave_hours < ZERO:
        raise LeavePayRefusedError(
            f"Leave cannot be negative: {data.leave_days} day(s), {data.leave_hours} "
            f"hour(s). A reversal is its own ledger row, not a negative payment."
        )
    if bool(data.leave_days) == bool(data.leave_hours):
        raise LeavePayRefusedError(
            f"Leave must be priced in exactly one unit: {data.leave_days} day(s) and "
            f"{data.leave_hours} hour(s) were both given. A balance is held in whatever "
            f"unit its own accrual produced and nothing in this system converts between "
            f"them (D-164)."
        )
    return data.leave_days or data.leave_hours


def _average_weekly(data: LeavePayInput) -> Decimal:
    window = data.window
    if window is None:
        raise LeavePayRefusedError(
            "This employee's remuneration is variable, so s35(4) requires the payment to "
            "be calculated by reference to the preceding weeks' remuneration, and no "
            "averaging window was supplied. There is no ordinary rate to fall back to: "
            "falling back is what s35(4) exists to prevent."
        )
    if window.weeks_available <= ZERO:
        raise LeavePayRefusedError(
            f"The averaging window is {window.weeks_available} weeks. s35(4)(b) shortens "
            f"it to the period of employment where that is shorter, but an employee with "
            f"no employment behind them has no remuneration to average."
        )
    if window.weeks_available > window.weeks.value:
        raise LeavePayRefusedError(
            f"The averaging window is {window.weeks_available} weeks and s35(4)(a) gives "
            f"{window.weeks.value}. Averaging over longer than the Act allows reaches back "
            f"past the period it names."
        )
    if window.remuneration < ZERO:
        raise LeavePayRefusedError(
            f"Remuneration over the averaging window is {window.remuneration}. A negative "
            f"total is a capture error upstream, not a rate."
        )
    return window.remuneration / window.weeks_available


def _rate_from_weekly(weekly: Decimal, per_week: Decimal, unit: str) -> Decimal:
    if per_week <= ZERO:
        raise LeavePayRefusedError(
            f"The weekly average is {weekly} and the employee works {per_week} {unit} a "
            f"week, so there is nothing to divide by. Weekly is the hub every other rate "
            f"comes off (D-106), and the pattern is on the employee's own work schedule."
        )
    return weekly / per_week


def leave_pay(data: LeavePayInput) -> LeavePayResult:
    """What one period of leave is worth."""
    warnings: list[str] = []
    quantity = _refuse_an_unpriceable_quantity(data)
    in_days = bool(data.leave_days)

    average_weekly: Decimal | None = None
    if data.remuneration_is_variable:
        average_weekly = _average_weekly(data)
        rate = (
            _rate_from_weekly(average_weekly, data.days_per_week, "days")
            if in_days
            else _rate_from_weekly(average_weekly, data.hours_per_week, "hours")
        )
        contractual = data.daily_rate if in_days else data.hourly_rate
        # Compared at the working precision, not raw: the contractual rate is
        # already stored quantized to six places (employee_remuneration's own
        # columns), and an unrounded average that agrees with it to the last
        # place is not "below" it. Comparing raw warns on every hourly employee
        # whose rate does not divide evenly.
        if contractual and Money.of(rate).exact < Money.of(contractual).exact:
            # s21(1) says "at least equivalent to", and (a) and (b) together say
            # how the figure is reached — (b) governs how (a)'s rate is found. So
            # this is NOT floored at the contractual rate: that would invent an
            # entitlement. It is said out loud instead.
            warnings.append(
                f"The s35(4) average of {rate} per {'day' if in_days else 'hour'} is below "
                f"the contractual rate of {contractual}. That is what averaging a "
                f"fluctuating wage over the preceding "
                f"{data.window.weeks_available} week(s) produced; s21(1)(b) makes s35 the "
                f"calculation, so it is not topped up here. Check the window."
            )
    else:
        rate = data.daily_rate if in_days else data.hourly_rate
        if rate <= ZERO:
            raise LeavePayRefusedError(
                f"The employee's {'daily' if in_days else 'hourly'} rate is {rate}. Leave "
                f"pay is the remuneration the employee would have received for working "
                f"that period (s21(1)), and there is no rate to pay it at."
            )

    amount = Money.of(quantity * rate)
    line = PayslipLine(
        component_code=COMPONENT,
        description="Annual leave pay",
        units=quantity,
        rate=rate,
        amount=amount,
    )

    window = data.window
    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            leave_days=data.leave_days,
            leave_hours=data.leave_hours,
            daily_rate=data.daily_rate,
            hourly_rate=data.hourly_rate,
            remuneration_is_variable=data.remuneration_is_variable,
            days_per_week=data.days_per_week,
            hours_per_week=data.hours_per_week,
            window_weeks=None if window is None else window.weeks.value,
            window_weeks_available=None if window is None else window.weeks_available,
            window_remuneration=None if window is None else window.remuneration,
        ),
        statutory_rows=rows_of(window) if window is not None else (),
        outputs=as_text(
            average_weekly=None if average_weekly is None else Money.of(average_weekly).exact,
            rate_used=Money.of(rate).exact,
            amount=amount.exact,
            used_the_average=data.remuneration_is_variable,
        ),
        warnings=tuple(warnings),
    )

    return LeavePayResult(
        line=line,
        amount=amount,
        rate_used=Money.of(rate),
        average_weekly=None if average_weekly is None else Money.of(average_weekly),
        used_the_average=data.remuneration_is_variable,
        trace=trace,
    )
