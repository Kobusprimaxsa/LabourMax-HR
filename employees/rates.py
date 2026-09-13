"""Rate derivation — one captured rate, three normalised ones.

Pure functions. No ORM, no I/O, no clock, and the statutory factor is **handed in**
rather than looked up, so this module obeys the ``calculators/`` rule even though it
does not live there yet. When the payroll engine arrives in P7 it can move without
changing, and it is testable against a worked example with no database at all.

**Why derive at all.** An employer captures whatever they think in: R30 an hour, or
R4,500 a month. Every calculator downstream — overtime, Sunday premium, public
holiday pay, leave pay, notice, severance, the minimum wage check — needs one
consistent basis, and having each of them convert would mean the same arithmetic
written eight times with eight chances to round differently. So it is done once,
here, and stored on the row.

**Why store rather than recompute.** The derived rates are columns on
``employee_remuneration``, not properties. Invariant 2: a March 2026 payroll re-run
in 2029 must reproduce March 2026. If the rates were recomputed on read they would
be recomputed with 2029's ``hours_per_week`` and 2029's statutory factor, and the
re-run would quietly differ from the payslip the employee was given.

**The one statutory figure.** BCEA s35 sets monthly remuneration at four and
one-third times weekly, so every conversion that crosses between a week and a month
passes through it. It is ``MONTHLY_TO_WEEKLY_FACTOR`` in ``statutory_parameter``
with its citation, and it arrives here as an argument (D-104).

**The conversion is not symmetric, and that is not a bug.** Hourly is derived from
weekly by dividing by ``hours_per_week``; daily by dividing by ``days_per_week``.
An employee on 45 hours over 5 days has a 9-hour day, so hourly × 9 = daily. An
employee on 40 hours over 5 days has an 8-hour day. Deriving daily as hourly ×
``hours_per_day`` when ``hours_per_day × days_per_week ≠ hours_per_week`` would give
a different answer from weekly ÷ ``days_per_week``, and the employee would be paid
one figure for a day's leave and another for a day's work. So **weekly is the hub**:
every basis converts to a weekly rate first, and hourly and daily both come off it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Derived rates are stored at six decimal places — NUMERIC(14,6) on the column.
#: A working precision, not a payslip figure: rounding to two decimals happens once,
#: at the payslip line, and nowhere else (invariant 6).
RATE_PRECISION = Decimal("0.000001")

#: Fortnightly is two weeks. A calendar fact, not a gazetted one.
WEEKS_PER_FORTNIGHT = 2


class RateDerivationError(ValueError):
    """The rate cannot be normalised. Nothing is returned."""


@dataclass(frozen=True)
class WorkingPattern:
    """What the employee actually works. Captured per employee, not statutory.

    These are the employee's own contracted hours. They default to the BCEA shape
    on the pay group (D-86) because that is the common case, but the statutory
    maximum lives in ``working_time_rule_set`` with its citation — these columns
    are a fact about one person's contract.
    """

    hours_per_day: Decimal
    days_per_week: Decimal
    hours_per_week: Decimal

    def validate(self):
        if self.hours_per_week <= 0:
            raise RateDerivationError(
                "Hours per week must be greater than zero, or an hourly rate cannot "
                "be derived from a weekly, fortnightly or monthly salary."
            )
        if self.days_per_week <= 0:
            raise RateDerivationError("Days per week must be greater than zero.")
        if self.hours_per_day <= 0:
            raise RateDerivationError("Hours per day must be greater than zero.")


@dataclass(frozen=True)
class DerivedRates:
    """The three normalised rates, plus the weekly rate they were all built from."""

    weekly: Decimal
    hourly: Decimal
    daily: Decimal
    monthly: Decimal


def _round(value: Decimal) -> Decimal:
    return value.quantize(RATE_PRECISION, rounding=ROUND_HALF_UP)


def weekly_rate_from(
    pay_basis: str, rate_amount: Decimal, pattern: WorkingPattern, monthly_factor: Decimal
) -> Decimal:
    """Convert any captured rate to a weekly one — the hub every other rate comes off.

    ``monthly_factor`` is BCEA s35's four and one-third, passed in rather than known
    here. A default would be a statutory figure with no citation, which is the whole
    thing this codebase refuses to have.
    """
    if rate_amount <= 0:
        raise RateDerivationError("A pay rate must be greater than zero.")
    if monthly_factor <= 0:
        raise RateDerivationError(
            "The monthly-to-weekly factor must be greater than zero. It comes from "
            "MONTHLY_TO_WEEKLY_FACTOR in statutory_parameter."
        )
    pattern.validate()

    if pay_basis == "hourly":
        return rate_amount * pattern.hours_per_week
    if pay_basis == "daily":
        return rate_amount * pattern.days_per_week
    if pay_basis == "weekly":
        return rate_amount
    if pay_basis == "fortnightly":
        return rate_amount / WEEKS_PER_FORTNIGHT
    if pay_basis == "monthly":
        return rate_amount / monthly_factor

    raise RateDerivationError(
        f"'{pay_basis}' is not a pay basis. The five are hourly, daily, weekly, "
        f"fortnightly and monthly."
    )


def derive(
    pay_basis: str, rate_amount: Decimal, pattern: WorkingPattern, monthly_factor: Decimal
) -> DerivedRates:
    """Normalise a captured rate into weekly, hourly, daily and monthly.

    The captured basis is preserved exactly: an employee captured at R4,500 a month
    gets a monthly rate of exactly 4500, not 4500 round-tripped through a weekly
    figure and back. Round-tripping would cost a few cents on most salaries and the
    employee would notice, because their contract says 4,500.
    """
    weekly = weekly_rate_from(pay_basis, rate_amount, pattern, monthly_factor)

    hourly = weekly / pattern.hours_per_week
    daily = weekly / pattern.days_per_week
    monthly = rate_amount if pay_basis == "monthly" else weekly * monthly_factor

    if pay_basis == "hourly":
        hourly = rate_amount
    if pay_basis == "daily":
        daily = rate_amount

    return DerivedRates(
        weekly=_round(weekly),
        hourly=_round(hourly),
        daily=_round(daily),
        monthly=_round(monthly),
    )
