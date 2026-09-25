"""The statutory annual bonus: one employee, one bonus cycle, as at one date.

Pure. The rule arrives off the ``termination_rule_set`` row the caller resolved
— weeks, payment month, pro rata on termination, minimum service — carrying the
row's key; the wages arrive as the employee's own weekly figures; every date is
an input.

**Only two instruments in this build give one, and both are loaded as data.**
Sectoral Determination 1, clause 3(3) (substituted by Notice 687 of 2013):

    "An annual bonus will be paid to all employees, during the month of December
    or on termination of employment. This bonus will be calculated as follows—
    (a) An employee shall receive an annual bonus equivalent to four point three
    three three weeks of the employee's weekly wage as from 1 December 2012.
    (b) Subject to paragraph (a) an employee who has not been in employment for a
    period of 12 months shall be paid a prorated bonus calculated as follows:
    (i) The number of full calendar months service divided by 12 and multiplied
    by four point three three three times the employee's weekly wage."

and the BCCCI Main Agreement's clause 4.5 for KwaZulu-Natal — 4,33 weeks, not
4,333, pro-rated on full calendar months over twelve, with three of its rules
made employer elections by 4.5(g) (D-242). The domestic sector and the BCEA give
no bonus at all: their rule sets carry no payment month, and this function
REFUSES rather than computing a zero, so an employer without one gets no rows
rather than rows of nothing (``payroll/bonus.py``).

**One formula for both paragraphs.** Each full calendar month of service in the
cycle earns one twelfth of the weeks at that month's weekly wage. Twelve months
is (a); fewer is (b). There is no second path to disagree with the first.

**The cycle is the twelve calendar months ending with the payment month** —
January to December for both instruments. Neither states a cycle in terms: SD1
says only "during the month of December", and D-242 read BCCCI 4.5(d)'s
prevailing rate as "each month actually worked in that calendar year". So a
December payment prices the months of that calendar year, and a leaver in June
is paid for the full months of this year before they left. That reading is the
workbook's too (sheet 02, ``termination_payout.bonus_months_worked``: "Full
calendar months in the current bonus cycle").

**A leaver is paid PRO RATA, however long their service** - SETTLED 25 September
2026 by Kobus (D-320, closing O-44; an owner confirmation on D-117's precedent):
completed full calendar months of the CURRENT cycle, in both instruments. Read
literally, SD1's (b) pro-rates only "an employee who has not been in employment
for a period of 12 months", which would pay a long-serving June leaver the whole
of (a); the ruling is that it does not. The two instruments reach the answer
differently: SD1 clause 3(3) pays "during the month of December OR ON
TERMINATION OF EMPLOYMENT", so paying a leaver is what it REQUIRES; BCCCI clause
4.5 pays "to all cleaners in employment on the 1st December" with NO termination
limb, so paying a KwaZulu-Natal leaver is MORE generous than the agreement asks -
lawful, and Kobus's deliberate election, which is why
``annual_bonus_pro_rata_on_termination`` is TRUE on the Area B rows.

**A full calendar month is the whole of one**, the first to the last day. A
month joined after the 1st earns nothing unless the caller says the employer
has elected otherwise (BCCCI 4.5(c)(ii) under 4.5(g)); a month left before its
last day earns nothing, in both instruments.

**Which weekly wage.** SD1 says "the employee's weekly wage", read as the wage
in force at payment or termination (``RateBasis.AT_THE_END``). BCCCI 4.5(d)
prices each month at the rate prevailing for it (``RateBasis.EACH_MONTH``),
taken on the month's LAST day — a wage may not be reduced, so that is never the
lower of two rates in one month.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from collections.abc import Sequence
from decimal import Decimal

from calculators.base import ZERO, CalculationTrace, Money, as_text, rows_of

CALCULATOR = "bonus.annual_bonus"

#: Twelve months in a year — the calendar, as in ``calculators/paye.py``. The
#: statutory figure here is the WEEKS, which is the row's.
MONTHS_IN_YEAR = Decimal("12")


class BonusInputError(ValueError):
    """The bonus cannot be priced, and guessing would be worse."""


class RateBasis(enum.StrEnum):
    #: SD1 3(3): "the employee's weekly wage" — the wage at the end.
    AT_THE_END = "at_the_end"
    #: BCCCI 4.5(d): the rate prevailing for each month worked.
    EACH_MONTH = "each_month"


@dataclasses.dataclass(frozen=True)
class BonusRule:
    """``termination_rule_set``'s four bonus columns, off one row."""

    weeks: Decimal
    #: NULL on the row where the instrument gives no bonus at all.
    payment_month: int | None
    pro_rata_on_termination: bool
    min_service_months: int
    table: str
    row_id: int

    @property
    def gives_a_bonus(self) -> bool:
        return self.payment_month is not None and self.weeks > ZERO


@dataclasses.dataclass(frozen=True)
class WeeklyWage:
    """One ``employee_remuneration`` row, reduced to the weekly hub (D-106).
    ``effective_to`` is exclusive and None while current, as on the row."""

    effective_from: datetime.date
    effective_to: datetime.date | None
    weekly: Decimal

    def in_force_on(self, day: datetime.date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)


@dataclasses.dataclass(frozen=True)
class BonusInput:
    calculated_for: datetime.date
    rule: BonusRule
    cycle_start: datetime.date
    cycle_end: datetime.date
    #: The CURRENT engagement's start (D-103: a re-hire starts fresh).
    service_start: datetime.date
    #: The last day employed, or None while still employed.
    service_end: datetime.date | None
    #: Months that have fully elapsed by this date count. The December payment
    #: is priced as at the cycle's end; a termination as at its own date.
    as_at: datetime.date
    wages: Sequence[WeeklyWage]
    rate_basis: RateBasis = RateBasis.AT_THE_END
    #: BCCCI 4.5(c)(ii) under 4.5(g): the employer counts a joining month in full.
    part_first_month_counts: bool = False
    is_termination: bool = False
    #: BCCCI 4.5(f): a casual does not qualify unless the employer elected
    #: otherwise. Decided by the caller, which reads the contract type.
    qualifies: bool = True
    disqualified_because: str = ""


@dataclasses.dataclass(frozen=True)
class BonusResult:
    #: The first day of every month that earned a twelfth.
    months: tuple[datetime.date, ...]
    amount: Money
    trace: CalculationTrace

    @property
    def full_months(self) -> int:
        return len(self.months)


def _last_day(year: int, month: int) -> datetime.date:
    following = datetime.date(year + month // 12, month % 12 + 1, 1)
    return following - datetime.timedelta(days=1)


def cycle_containing(day: datetime.date, payment_month: int) -> tuple[datetime.date, datetime.date]:
    """The twelve calendar months ending with the payment month that ``day``
    falls in. December payment: 1 January to 31 December."""
    end_year = day.year if day.month <= payment_month else day.year + 1
    end = _last_day(end_year, payment_month)
    start_month = payment_month % 12 + 1
    start_year = end_year if start_month == 1 else end_year - 1
    return datetime.date(start_year, start_month, 1), end


def full_months_of_service(start: datetime.date, on: datetime.date) -> int:
    """Complete months from ``start`` to ``on``, for the minimum-service test."""
    months = (on.year - start.year) * 12 + (on.month - start.month)
    return max(months - (1 if on.day < start.day else 0), 0)


def counted_months(data: BonusInput) -> tuple[datetime.date, ...]:
    months = []
    first = datetime.date(data.cycle_start.year, data.cycle_start.month, 1)
    while first <= data.cycle_end:
        last = _last_day(first.year, first.month)
        joined_in_time = data.service_start <= first or (
            data.part_first_month_counts and data.service_start <= last
        )
        stayed_to_the_end = data.service_end is None or data.service_end >= last
        if joined_in_time and stayed_to_the_end and last <= data.as_at:
            months.append(first)
        first = last + datetime.timedelta(days=1)
    return tuple(months)


def _wage_on(data: BonusInput, day: datetime.date) -> Decimal:
    for wage in data.wages:
        if wage.in_force_on(day):
            return wage.weekly
    raise BonusInputError(
        f"No weekly wage is in force on {day:%d %B %Y}, a day this employee's bonus is "
        f"priced on. The remuneration history has a gap; a bonus priced across it would "
        f"be priced on nothing."
    )


def wage_as_text(wage: WeeklyWage) -> str:
    return f"{wage.effective_from.isoformat()}|{wage.effective_to or ''}|{wage.weekly}"


def annual_bonus(data: BonusInput) -> BonusResult:
    """What the cycle's bonus is worth as at ``as_at``."""
    rule = data.rule
    if not rule.gives_a_bonus:
        raise BonusInputError(
            f"{rule.table}.{rule.row_id} gives no annual bonus (payment month "
            f"{rule.payment_month}, {rule.weeks} weeks). The domestic sector and the BCEA "
            f"have none, and there is nothing to accrue — which is different from a bonus "
            f"of nil, and must not be stored as one."
        )
    if data.cycle_end < data.cycle_start:
        raise BonusInputError(
            f"The bonus cycle ends on {data.cycle_end:%d %B %Y}, before it starts."
        )

    warnings: list[str] = []
    months = counted_months(data)
    if len(months) > MONTHS_IN_YEAR:
        raise BonusInputError(
            f"{len(months)} months counted in one cycle. A cycle is twelve calendar months."
        )

    if data.is_termination and not rule.pro_rata_on_termination:
        warnings.append(
            "The instrument in force pays no pro-rata bonus on termination; the months "
            "served in this cycle earn nothing."
        )
        months = ()
    elif not data.qualifies:
        warnings.append(f"No bonus: {data.disqualified_because or 'does not qualify'}.")
        months = ()
    elif full_months_of_service(data.service_start, data.as_at) < rule.min_service_months:
        warnings.append(
            f"No bonus: {full_months_of_service(data.service_start, data.as_at)} month(s) of "
            f"service, and the instrument requires {rule.min_service_months}."
        )
        months = ()

    at_the_end = data.service_end if data.service_end is not None else data.as_at
    earned = ZERO
    for first in months:
        priced_on = (
            _last_day(first.year, first.month)
            if data.rate_basis is RateBasis.EACH_MONTH
            else min(at_the_end, data.as_at)
        )
        earned += _wage_on(data, priced_on) * rule.weeks / MONTHS_IN_YEAR

    amount = Money.of(earned)
    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            weeks=rule.weeks,
            payment_month=rule.payment_month,
            pro_rata_on_termination=rule.pro_rata_on_termination,
            min_service_months=rule.min_service_months,
            cycle_start=data.cycle_start,
            cycle_end=data.cycle_end,
            service_start=data.service_start,
            service_end=data.service_end,
            as_at=data.as_at,
            rate_basis=data.rate_basis.value,
            part_first_month_counts=data.part_first_month_counts,
            is_termination=data.is_termination,
            qualifies=data.qualifies,
            disqualified_because=data.disqualified_because,
        )
        | {f"wage_{index:02d}": wage_as_text(wage) for index, wage in enumerate(data.wages, 1)},
        statutory_rows=rows_of(rule),
        outputs=as_text(full_months=len(months), amount=amount.exact),
        warnings=tuple(warnings),
    )
    return BonusResult(months=tuple(months), amount=amount, trace=trace)
