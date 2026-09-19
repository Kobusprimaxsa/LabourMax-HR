"""BCEA s35 — "Calculation of remuneration and wages". One rule, three payments.

s35(5) says so itself: it governs "calculating an employee's annual leave pay in
terms of section 21, notice pay in terms of section 38 or severance pay in terms
of section 41", and the Minister's determination under it (Government Notice 691
of 23 May 2003) repeats the same three. So the rate lives here rather than
inside whichever calculator needed it first — three copies of an averaging rule
is three answers to one question.

**s35(4)** is the whole of it: "If an employee's remuneration or wage is
calculated, either wholly or in part, on a basis other than time or if an
employee's remuneration or wage fluctuates significantly from period to period,
any payment to that employee in terms of this Act must be calculated by
reference to the employee's remuneration or wage during — (a) the preceding 13
weeks; or (b) if the employee has been in employment for a shorter period, that
period."

**Whether it applies is DECLARED** (D-110's shape, D-220). "On a basis other
than time" is a fact about the contract, and "fluctuates significantly" has no
statutory threshold anywhere — no gazette says what significant means, so
nothing here may invent one.

**Weekly is the hub** (D-106). The average is a weekly figure — remuneration
over the window, divided by the window — and the daily and hourly rates come off
it by the employee's own days and hours per week. Deriving daily as hourly ×
hours-per-day disagrees with weekly ÷ days-per-week whenever the two do not
reconcile, and the employee would be paid one figure for a day of leave and
another for a day of work.

**What counts as remuneration is not decided here.** s35(5) and GN 691 list what
is in and what is out; in this codebase that is
``payroll_component.affects_leave_pay_average``, decided once per component, and
the caller hands in the already-filtered total.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from calculators.base import ZERO, Money, StatutoryFigure


class RemunerationRefusedError(ValueError):
    """The s35 rate cannot be worked out, and guessing would be worse."""


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
class Section35Rates:
    """The rates a payment under the Act is calculated at.

    ``daily`` and ``hourly`` are None where the employee's working pattern does
    not carry the days or hours per week to derive them. A payment that needs
    one then refuses through ``per_day()`` or ``per_hour()`` — rather than this
    function refusing up front for a rate the caller was never going to use.
    """

    weekly: Decimal
    daily: Decimal | None
    hourly: Decimal | None
    used_the_average: bool
    #: The s35(4) average, where one was used. None on the ordinary path.
    average_weekly: Decimal | None
    warnings: tuple[str, ...] = ()

    def per_day(self) -> Decimal:
        if self.daily is None:
            raise RemunerationRefusedError(
                f"The weekly figure is {self.weekly} and the employee's schedule carries no "
                f"days per week, so there is nothing to divide by. Weekly is the hub every "
                f"other rate comes off (D-106)."
            )
        return self.daily

    def per_hour(self) -> Decimal:
        if self.hourly is None:
            raise RemunerationRefusedError(
                f"The weekly figure is {self.weekly} and the employee's schedule carries no "
                f"hours per week, so there is nothing to divide by. Weekly is the hub every "
                f"other rate comes off (D-106)."
            )
        return self.hourly


def _weekly_average(window: AveragingWindow | None) -> Decimal:
    if window is None:
        raise RemunerationRefusedError(
            "This employee's remuneration is variable, so s35(4) requires the payment to "
            "be calculated by reference to the preceding weeks' remuneration, and no "
            "averaging window was supplied. There is no ordinary rate to fall back to: "
            "falling back is what s35(4) exists to prevent."
        )
    if window.weeks_available <= ZERO:
        raise RemunerationRefusedError(
            f"The averaging window is {window.weeks_available} weeks. s35(4)(b) shortens "
            f"it to the period of employment where that is shorter, but an employee with "
            f"no employment behind them has no remuneration to average."
        )
    if window.weeks_available > window.weeks.value:
        raise RemunerationRefusedError(
            f"The averaging window is {window.weeks_available} weeks and s35(4)(a) gives "
            f"{window.weeks.value}. Averaging over longer than the Act allows reaches back "
            f"past the period it names."
        )
    if window.remuneration < ZERO:
        raise RemunerationRefusedError(
            f"Remuneration over the averaging window is {window.remuneration}. A negative "
            f"total is a capture error upstream, not a rate."
        )
    return window.remuneration / window.weeks_available


def _per(weekly: Decimal, per_week: Decimal) -> Decimal | None:
    """None where the pattern does not say — see ``Section35Rates``."""
    if per_week <= ZERO:
        return None
    return weekly / per_week


def section_35_rates(
    *,
    contractual_weekly: Decimal,
    contractual_daily: Decimal,
    contractual_hourly: Decimal,
    remuneration_is_variable: bool,
    window: AveragingWindow | None = None,
    days_per_week: Decimal = ZERO,
    hours_per_week: Decimal = ZERO,
) -> Section35Rates:
    """The rate any s21, s38 or s41 payment is calculated at."""
    if not remuneration_is_variable:
        return Section35Rates(
            weekly=contractual_weekly,
            daily=contractual_daily,
            hourly=contractual_hourly,
            used_the_average=False,
            average_weekly=None,
        )

    weekly = _weekly_average(window)
    daily = _per(weekly, days_per_week)
    hourly = _per(weekly, hours_per_week)

    warnings: list[str] = []
    for average, contractual, unit in (
        (daily, contractual_daily, "day"),
        (hourly, contractual_hourly, "hour"),
    ):
        if average is None:
            continue
        # Compared at the working precision, not raw: the contractual rate is
        # already stored quantized to six places (employee_remuneration's own
        # columns), and an unrounded average that agrees with it to the last
        # place is not "below" it. Comparing raw warns on every hourly employee
        # whose rate does not divide evenly.
        if contractual and Money.of(average).exact < Money.of(contractual).exact:
            # s21(1) and s38(1) both say "at least"; limbs (a) and (b) of s21(1)
            # together say how the figure is reached, and (b) makes s35 the
            # calculation. So this is NOT floored at the contractual rate —
            # that would invent an entitlement. It is said out loud instead.
            warnings.append(
                f"The s35(4) average of {Money.of(average).exact} per {unit} is below the "
                f"contractual rate of {contractual}. That is what averaging over the "
                f"preceding {window.weeks_available} week(s) produced; s35 is the "
                f"calculation, so it is not topped up here. Check the window."
            )

    return Section35Rates(
        weekly=weekly,
        daily=daily,
        hourly=hourly,
        used_the_average=True,
        average_weekly=weekly,
        warnings=tuple(warnings),
    )
