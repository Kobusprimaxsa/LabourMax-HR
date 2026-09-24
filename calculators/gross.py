"""Gross pay — one calculator, five pay bases. BCEA ss 10, 16, 17, 18, SD1, SD7.

Pure. The hours arrive already bucketed by ``calculators/attendance.py``; this
module only prices them. Every multiplier arrives carrying the key of the
``working_time_rule_set`` row the caller read, and the employee's own rates
arrive as plain Decimals — they are that person's contract, not a gazetted
figure.

**Every premium here was written from the section's own words, and two of them
are not what a multiplier column suggests.** The sections were read, not
inferred, and the difference was material both times:

* **s10(2) is per HOUR** — "at least one and one-half times the employee's wage
  for overtime worked". A plain hours × wage × 1.5.
* **s16(1) is per HOUR** — "double the employee's wage for each hour worked,
  unless the employee ordinarily works on a Sunday, in which case ... one and
  one-half times". But **s16(2) puts a floor under it**: "If an employee works
  less than the employee's ordinary shift on a Sunday and the payment that the
  employee is entitled to in terms of subsection (1) is less than the employee's
  ordinary daily wage, the employer must pay the employee the employee's
  ordinary daily wage." Two hours on a Sunday is a day's pay, not two hours'.
* **s18 is per DAY, not per hour.** s18(2)(b) pays an employee who works a
  public holiday falling on a day they would ordinarily work "(i) at least
  double the amount referred to in paragraph (a)" — and (a) is "the wage that
  the employee would ordinarily have received for work on that day", a DAY's
  wage. Or "(ii) if it is greater, the amount referred to in paragraph (a) plus
  the amount earned by the employee for the time worked on that day". So four
  hours on a public holiday is two days' wages, not eight hours' — pricing
  ``hours × wage × 2`` underpays every short public holiday shift.
* **s18(3) is a different rule again**, for a public holiday the employee would
  NOT ordinarily work: "an amount equal to (a) the employee's ordinary daily
  wage; plus (b) the amount earned by the employee for the work performed that
  day". A day's wage PLUS the hours, and no doubling.

**The one rule that makes five bases into one calculator.** Each of those
sections states a TOTAL for the day, so the premium line is the total less what
the basic pay already paid for that day:

    premium = max(statutory entitlement for the day − what BASIC already paid, 0)

What the basic already paid is a fact about the pay basis, not a judgement:

* **hourly** — the ordinary bucket plus the short-day guarantee, at the hourly
  rate. ``bucket_day()`` keeps the four worked buckets disjoint, so a Sunday or
  public holiday hour is never in it, and the basic paid nothing for that day.
* **daily** — ``days_worked_equivalent`` × the daily rate. A Sunday or public
  holiday worked counts toward that, so the basic did pay for it.
* **weekly / fortnightly / monthly** — the salary buys every ORDINARY working
  day, so it paid one ordinary daily wage for a day the employee ordinarily
  works and nothing for one they do not.

Run the same Sunday through all three and they agree, which is the test that
matters: an employee moved from hourly to salaried for the same work must not be
paid a different amount.

**A public holiday NOT worked is BASIC, not a premium** — s18(2)(a), and the
component catalogue's own reasoning under ``PH_WORKED``. ``days_worked_equivalent``
is 0.000 for a day with no hours in it, so an hourly or daily employee would
otherwise be paid nothing for a holiday they were entitled to be paid for.

**Leave is not priced here.** A LEAVE day contributes no worked hours, so an
hourly or daily employee's basic correctly excludes it — ``LEAVE_PAY`` is its
own component with its own variable-earnings average (D-166). A salaried
employee on leave is paid their salary and nothing is deducted, which is what
happens here by construction.

**Standby is REFUSED, deliberately** (O-19, O-20, O-22). Three things are
unanswered: whether the standby window columns are meant to be consulted at all,
how a day that is both an ordinary shift and a standby shift is represented,
and — the one that would bite here — whether ``standby_allowance_per_shift`` of
0.00 means "this instrument states no allowance" or "the allowance is nil". The
night allowance had that same sentinel and it was fixed before this module was
written (O-22's night half); O-22 records that the standby columns cannot be
settled until O-19 and O-20 are.

**An employee above the BCEA earnings threshold is REFUSED** where the period
carries overtime, Sunday or night hours (O-24). s6(3) requires the Minister to
determine which provisions fall away above a stated amount, and that
determination — not the Act — carries the list. It has not been read into this
build. s18(3) is the one exclusion this codebase has already settled, so it is
applied; the rest refuse rather than paying a premium that may not be owed.
Neither of our sectors comes near the threshold, so this costs nothing real.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from collections.abc import Sequence
from decimal import Decimal

from calculators.attendance import AttendanceDayInput, AttendanceDayResult, DayType
from calculators.base import (
    ZERO,
    CalculationTrace,
    Money,
    PayslipLine,
    as_text,
    rows_of,
)

CALCULATOR = "gross.gross_pay"

PER_CENT = Decimal("100")
ONE = Decimal("1")


class PayBasis(enum.StrEnum):
    """The five ``employee_remuneration`` bases, as ``employees/rates.py`` names
    them. Copied rather than imported — that module is not under
    ``calculators/`` — and held to it by a test in ``payroll/``."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"


#: The bases whose basic pay is computed FROM the attendance rows (D-25's own
#: ``is_attendance_driven``). The other three are a fixed amount for the period.
ATTENDANCE_DRIVEN = frozenset({PayBasis.HOURLY, PayBasis.DAILY})


class NightAllowanceKind(enum.StrEnum):
    """Mirrors ``statutory.NightAllowanceType``, value for value (O-22).
    ``payroll/tests/test_gross_boundary.py`` asserts the two never drift."""

    PERCENTAGE = "percentage"
    FIXED_AMOUNT = "fixed_amount"
    TIME_OFF = "time_off"
    BY_AGREEMENT = "by_agreement"


class GrossPayRefusedError(ValueError):
    """The period cannot be priced, and guessing would be worse."""


@dataclasses.dataclass(frozen=True)
class PremiumRates:
    """The pricing columns of one ``working_time_rule_set`` row, as in force on
    the work date. One row, so the key is carried once (``base.Sourced``)."""

    overtime_multiplier: Decimal
    sunday_multiplier_ordinary: Decimal
    sunday_multiplier_non_ordinary: Decimal
    #: s18(2)(b)(i)'s "double the amount referred to in paragraph (a)" — and (a)
    #: is a DAY's wage. This multiplies the daily rate, never the hours.
    public_holiday_worked_multiplier: Decimal
    public_holiday_not_worked_paid: bool
    night_allowance_type: NightAllowanceKind
    #: NULL for time_off and by_agreement. The absence of a figure, never a zero
    #: standing in for it (O-22).
    night_allowance_value: Decimal | None
    table: str
    row_id: int

    def __post_init__(self):
        for value in (
            self.overtime_multiplier,
            self.sunday_multiplier_ordinary,
            self.sunday_multiplier_non_ordinary,
            self.public_holiday_worked_multiplier,
        ):
            if not isinstance(value, Decimal):
                raise TypeError(
                    f"{self.table}.{self.row_id} carries {type(value).__name__}, not "
                    "Decimal. Money is Decimal everywhere in this system (invariant 6)."
                )

    def sunday_multiplier(self, *, ordinarily_works_sundays: bool) -> Decimal:
        """s16(1): time and a half when Sunday IS an ordinary working day for
        this employee, double when it is not. The distinction is per employee,
        which is why one stored multiplier could only ever be half right."""
        return (
            self.sunday_multiplier_ordinary
            if ordinarily_works_sundays
            else self.sunday_multiplier_non_ordinary
        )


@dataclasses.dataclass(frozen=True)
class DayPay:
    """One day, as captured and as bucketed. Both halves, because the pricing
    needs schedule facts — the day type, whether it is an ordinary working
    day — that the bucketed result deliberately does not carry."""

    day: AttendanceDayInput
    hours: AttendanceDayResult


@dataclasses.dataclass(frozen=True)
class GrossInput:
    calculated_for: datetime.date
    pay_basis: PayBasis
    days: Sequence[DayPay]
    rates: PremiumRates
    #: The employee's own derived rates, from ``employee_remuneration``. Not
    #: statutory: this is one person's contract (D-104, D-106).
    hourly_rate: Decimal
    daily_rate: Decimal
    #: ``work_schedule.hours_per_day`` — the employee's ordinary shift, for
    #: s16(2)'s "works less than the employee's ordinary shift on a Sunday".
    #: Not the weekday's scheduled hours, which are zero on a day off.
    ordinary_shift_hours: Decimal
    #: What the salary pays for this period. Zero on an attendance-driven basis.
    period_rate: Decimal = ZERO
    #: ``pay_period.working_days_in_period``. Only read on a salaried basis, to
    #: pro-rate an unpaid absence.
    working_days_in_period: Decimal = ZERO
    #: BCEA s6(3). A declared boolean (D-110): the employer states which side of
    #: the line the person is on, and the threshold stays reference data.
    above_bcea_earnings_threshold: bool = False
    #: Unpaid working days a salary is pro-rated for that are NOT ``absent_unpaid``
    #: attendance rows: the unpaid portion of approved leave (a half day is 0.5),
    #: and the working days of the period before the engagement began or after
    #: it ended. The caller counts them; this module only pro-rates (D-293).
    further_unpaid_days: Decimal = ZERO


@dataclasses.dataclass(frozen=True)
class GrossResult:
    lines: tuple[PayslipLine, ...]
    gross: Money
    trace: CalculationTrace


def day_as_text(pay: DayPay) -> str:
    """One day, as the trace records it: every fact about the day this module
    prices from, and nothing else, in a fixed order.

    The trace used to record ``days=len(data.days)`` and no day at all, so a
    gross figure could not be reproduced from its trace — only from the
    attendance rows, which a reversed run is allowed to delete. The property
    that replays every trace found it (D-284). Pipe-separated rather than
    nested, because ``CalculationTrace.inputs`` is a flat mapping of strings
    and a stored trace must read the same in 2029.
    """
    day, hours = pay.day, pay.hours
    return "|".join(
        str(value)
        for value in (
            day.work_date.isoformat(),
            day.day_type,
            day.is_ordinary_working_day,
            day.is_standby,
            day.scheduled_ordinary_hours,
            hours.ordinary_hours,
            hours.overtime_hours,
            hours.sunday_hours,
            hours.public_holiday_hours,
            hours.night_hours,
            hours.paid_hours_guaranteed,
            hours.standby_hours_worked,
            hours.days_worked_equivalent,
        )
    )


# ------------------------------------------------------------------- refusals


def _refuse_standby(days: Sequence[DayPay]) -> None:
    standby = [
        pay.day.work_date for pay in days if pay.day.is_standby or pay.hours.standby_hours_worked
    ]
    if not standby:
        return
    raise GrossPayRefusedError(
        f"{len(standby)} standby day(s) in this period, the first on {standby[0]}, and "
        f"standby pay is not priced. Three things are unanswered: whether the standby "
        f"window columns are meant to be consulted at all (O-20), how a day that is both "
        f"an ordinary shift and a standby shift is represented (O-19), and whether "
        f"standby_allowance_per_shift of 0.00 means the instrument states no allowance or "
        f"that the allowance is nil (O-22). Paying against that figure would be guessing."
    )


def _refuse_unread_threshold_exclusions(data: GrossInput) -> None:
    if not data.above_bcea_earnings_threshold:
        return
    excluded = [
        name
        for name, total in (
            ("overtime", sum((pay.hours.overtime_hours for pay in data.days), ZERO)),
            ("Sunday", sum((pay.hours.sunday_hours for pay in data.days), ZERO)),
            ("night", sum((pay.hours.night_hours for pay in data.days), ZERO)),
        )
        if total
    ]
    if not excluded:
        return
    raise GrossPayRefusedError(
        f"This employee is above the BCEA earnings threshold and the period carries "
        f"{', '.join(excluded)} hours. s6(3) has the Minister determine which provisions "
        f"fall away above the threshold, and that determination — not the Act — carries "
        f"the list. It has not been read into this build (O-24), so whether s10, s16 and "
        f"s17(2) are owed here is unknown. s18(3) is the one exclusion already settled. "
        f"Read the determination before paying this period."
    )


# --------------------------------------------------------------- what is owed


def _is_a_paid_holiday_not_worked(pay: DayPay, rates: PremiumRates) -> bool:
    """s18(2)(a): a public holiday falling on a day the employee would
    ordinarily work is paid whether or not it is worked. Zero worked hours is
    what makes it the (a) case rather than the (b) one."""
    return (
        pay.day.day_type == DayType.PUBLIC_HOLIDAY
        and rates.public_holiday_not_worked_paid
        and pay.day.is_ordinary_working_day
        and pay.hours.public_holiday_hours == ZERO
    )


def _basic_hours(pay: DayPay, rates: PremiumRates) -> Decimal:
    """Hours the hourly basic pays for: the ordinary ones, the short-day
    guarantee, and a public holiday the employee did not work but is owed."""
    hours = pay.hours.ordinary_hours + pay.hours.paid_hours_guaranteed
    if _is_a_paid_holiday_not_worked(pay, rates):
        hours += pay.day.scheduled_ordinary_hours
    return hours


def _basic_days(pay: DayPay, rates: PremiumRates) -> Decimal:
    """Days the daily basic pays for. ``days_worked_equivalent`` is 0.000 for a
    day with no hours in it, so the unworked public holiday is added back."""
    if _is_a_paid_holiday_not_worked(pay, rates):
        return ONE
    return pay.hours.days_worked_equivalent


def _already_paid_for(pay: DayPay, data: GrossInput) -> Decimal:
    """What the BASIC line already paid for this one day. Mirrors the basic
    computation exactly — if these two disagree the premium is wrong by the
    difference, in one direction or the other."""
    if data.pay_basis is PayBasis.HOURLY:
        return _basic_hours(pay, data.rates) * data.hourly_rate
    if data.pay_basis is PayBasis.DAILY:
        return _basic_days(pay, data.rates) * data.daily_rate
    return data.daily_rate if pay.day.is_ordinary_working_day else ZERO


def _sunday_entitlement(pay: DayPay, data: GrossInput) -> Decimal:
    """s16(1), floored by s16(2)."""
    multiplier = data.rates.sunday_multiplier(
        ordinarily_works_sundays=pay.day.is_ordinary_working_day
    )
    entitlement = pay.hours.sunday_hours * data.hourly_rate * multiplier
    worked_less_than_a_shift = pay.hours.sunday_hours < data.ordinary_shift_hours
    if worked_less_than_a_shift and entitlement < data.daily_rate:
        return data.daily_rate
    return entitlement


def _public_holiday_entitlement(pay: DayPay, data: GrossInput) -> Decimal:
    """s18(2)(b) on a day the employee would ordinarily work, s18(3) otherwise."""
    earned = pay.hours.public_holiday_hours * data.hourly_rate
    if pay.day.is_ordinary_working_day:
        return max(
            data.rates.public_holiday_worked_multiplier * data.daily_rate,
            data.daily_rate + earned,
        )
    if data.above_bcea_earnings_threshold:
        # s18(3) is the exclusion this codebase has settled: the high earner
        # keeps the rest of s18 and loses this one, so the statute adds nothing
        # to what the hours themselves earned.
        return earned
    return data.daily_rate + earned


# ------------------------------------------------------------------ the basic


def _salaried_basic(data: GrossInput) -> tuple[Decimal, Decimal]:
    """The salary, less a pro-rata share for each unpaid day. Returns the
    fraction of the period earned and the unpaid day count."""
    unpaid = (
        Decimal(sum(1 for pay in data.days if pay.day.day_type == DayType.ABSENT_UNPAID))
        + data.further_unpaid_days
    )
    if not unpaid:
        return ONE, ZERO
    if data.working_days_in_period <= ZERO:
        raise GrossPayRefusedError(
            f"{unpaid} unpaid day(s) in this period and working_days_in_period is "
            f"{data.working_days_in_period}. A salaried employee's unpaid absence is "
            f"pro-rated over the period's own working days, and there is nothing to "
            f"divide by. It comes from pay_period.working_days_in_period."
        )
    if unpaid > data.working_days_in_period:
        raise GrossPayRefusedError(
            f"{unpaid} unpaid day(s) captured in a period with only "
            f"{data.working_days_in_period} working day(s). Pro-rating that would pay a "
            f"negative salary; the attendance or the period is wrong."
        )
    return (data.working_days_in_period - unpaid) / data.working_days_in_period, unpaid


def _night_line(data: GrossInput, warnings: list[str]) -> PayslipLine | None:
    """BCEA s17(2)(a). Only SD1 states a figure for it."""
    rates = data.rates
    night_hours = sum((pay.hours.night_hours for pay in data.days), ZERO)
    if not night_hours:
        return None
    shifts = Decimal(sum(1 for pay in data.days if pay.hours.night_hours > ZERO))

    if rates.night_allowance_type is NightAllowanceKind.PERCENTAGE:
        rate = data.hourly_rate * rates.night_allowance_value / PER_CENT
        return PayslipLine(
            component_code="NIGHT_ALLOW",
            description="Night work allowance",
            units=night_hours,
            rate=rate,
            amount=Money.of(night_hours * rate),
        )
    if rates.night_allowance_type is NightAllowanceKind.FIXED_AMOUNT:
        return PayslipLine(
            component_code="NIGHT_ALLOW",
            description="Night work allowance",
            units=shifts,
            rate=rates.night_allowance_value,
            amount=Money.of(shifts * rates.night_allowance_value),
        )
    if rates.night_allowance_type is NightAllowanceKind.TIME_OFF:
        # s17(2)(a)'s other limb: the obligation is discharged by reducing
        # working hours, so there is nothing to pay and nothing is owing.
        return None

    warnings.append(
        f"{night_hours} night hour(s) worked and the instrument in force states no "
        f"allowance. BCEA s17(2)(a) requires one, set by agreement between the parties, "
        f"so nothing is paid here because no figure is reference data. Check the "
        f"employee's own agreement."
    )
    return None


def _premium_line(
    code: str, description: str, hours: Decimal, amount: Decimal
) -> PayslipLine | None:
    if not hours or amount <= ZERO:
        return None
    return PayslipLine(
        component_code=code,
        description=description,
        units=hours,
        rate=amount / hours,
        amount=Money.of(amount),
    )


def gross_pay(data: GrossInput) -> GrossResult:
    """Gross earnings for one employee for one pay period."""
    warnings: list[str] = []
    _refuse_standby(data.days)
    _refuse_unread_threshold_exclusions(data)

    rates = data.rates
    lines: list[PayslipLine] = []

    if data.pay_basis is PayBasis.HOURLY:
        units = sum((_basic_hours(pay, rates) for pay in data.days), ZERO)
        rate = data.hourly_rate
    elif data.pay_basis is PayBasis.DAILY:
        units = sum((_basic_days(pay, rates) for pay in data.days), ZERO)
        rate = data.daily_rate
    else:
        units, unpaid = _salaried_basic(data)
        rate = data.period_rate
        if unpaid:
            warnings.append(
                f"{unpaid} unpaid day(s) pro-rated over {data.working_days_in_period} "
                f"working day(s) in the period."
            )

    lines.append(
        PayslipLine(
            component_code="BASIC",
            description="Basic wage",
            units=units,
            rate=rate,
            amount=Money.of(units * rate),
        )
    )

    # s10(2), per hour, and outside every basic on every basis: the hourly basic
    # is the ordinary bucket, the daily equivalent is capped at a full day, and
    # a salary buys the ordinary week.
    overtime_hours = sum((pay.hours.overtime_hours for pay in data.days), ZERO)
    overtime = _premium_line(
        "OT_1_5",
        "Overtime",
        overtime_hours,
        overtime_hours * data.hourly_rate * rates.overtime_multiplier,
    )
    if overtime is not None:
        lines.append(overtime)

    sunday_hours = sunday_amount = ZERO
    holiday_hours = holiday_amount = ZERO
    for pay in data.days:
        if pay.hours.sunday_hours:
            sunday_hours += pay.hours.sunday_hours
            sunday_amount += max(
                _sunday_entitlement(pay, data) - _already_paid_for(pay, data), ZERO
            )
        if pay.hours.public_holiday_hours:
            holiday_hours += pay.hours.public_holiday_hours
            holiday_amount += max(
                _public_holiday_entitlement(pay, data) - _already_paid_for(pay, data), ZERO
            )

    sunday = _premium_line("SUNDAY_2_0", "Sunday work", sunday_hours, sunday_amount)
    if sunday is not None:
        lines.append(sunday)
    holiday = _premium_line("PH_WORKED", "Public holiday worked", holiday_hours, holiday_amount)
    if holiday is not None:
        lines.append(holiday)

    night = _night_line(data, warnings)
    if night is not None:
        lines.append(night)

    for pay in data.days:
        warnings.extend(pay.hours.warnings)

    gross = Money.of(sum((line.amount.exact for line in lines), ZERO))

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            pay_basis=data.pay_basis.value,
            days=len(data.days),
            hourly_rate=data.hourly_rate,
            daily_rate=data.daily_rate,
            ordinary_shift_hours=data.ordinary_shift_hours,
            period_rate=data.period_rate,
            working_days_in_period=data.working_days_in_period,
            above_bcea_earnings_threshold=data.above_bcea_earnings_threshold,
            further_unpaid_days=data.further_unpaid_days,
            overtime_multiplier=rates.overtime_multiplier,
            sunday_multiplier_ordinary=rates.sunday_multiplier_ordinary,
            sunday_multiplier_non_ordinary=rates.sunday_multiplier_non_ordinary,
            public_holiday_worked_multiplier=rates.public_holiday_worked_multiplier,
            night_allowance_type=rates.night_allowance_type.value,
            night_allowance_value=rates.night_allowance_value,
        )
        | {f"day_{index:02d}": day_as_text(pay) for index, pay in enumerate(data.days, 1)},
        statutory_rows=rows_of(rates),
        outputs=as_text(
            gross=gross.exact,
            **{f"line_{line.component_code}": line.amount.exact for line in lines},
        ),
        warnings=tuple(warnings),
    )

    return GrossResult(lines=tuple(lines), gross=gross, trace=trace)
