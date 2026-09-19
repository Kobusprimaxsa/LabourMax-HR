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
    as_text,
    rows_of,
)
from calculators.remuneration import (
    AveragingWindow,
    RemunerationRefusedError,
    section_35_rates,
)

#: Re-exported: an ``AveragingWindow`` is s35(4)'s, not leave pay's, and the two
#: other payments it governs (s38 notice, s41 severance) read it from the same
#: place. Kept importable from here because leave pay is where it first landed.
__all__ = [
    "AveragingWindow",
    "LeavePayInput",
    "LeavePayRefusedError",
    "LeavePayResult",
    "leave_pay",
]

CALCULATOR = "leave_pay.leave_pay"

COMPONENT = "LEAVE_PAY"


class LeavePayRefusedError(RemunerationRefusedError):
    """The leave cannot be priced, and guessing would be worse.

    Subclasses the s35 refusal rather than sitting beside it: every way the rate
    itself can fail is also a way this calculation fails, and a caller should
    not have to catch two exceptions to find that out.
    """


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


def leave_pay(data: LeavePayInput) -> LeavePayResult:
    """What one period of leave is worth."""
    warnings: list[str] = []
    quantity = _refuse_an_unpriceable_quantity(data)
    in_days = bool(data.leave_days)

    rates = section_35_rates(
        contractual_weekly=ZERO,
        contractual_daily=data.daily_rate,
        contractual_hourly=data.hourly_rate,
        remuneration_is_variable=data.remuneration_is_variable,
        window=data.window,
        days_per_week=data.days_per_week,
        hours_per_week=data.hours_per_week,
    )
    average_weekly = rates.average_weekly
    rate = rates.per_day() if in_days else rates.per_hour()
    warnings.extend(
        warning for warning in rates.warnings if f"per {'day' if in_days else 'hour'}" in warning
    )

    if not data.remuneration_is_variable and rate <= ZERO:
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
