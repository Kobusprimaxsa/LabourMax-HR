"""Generating a year of pay periods from a pay group's calendar.

P3's definition of done is that an employer completes onboarding and a full year of
periods falls out. This is that.

**Periods are assigned to a tax year by PAYMENT DATE**, because that is what the
workbook says decides which year earnings fall into, and it is the reading that
matches how SARS sees it: a weekly period ending 27 February that pays on 3 March is
earnings in the new tax year. Assigning by period end would move a week of pay onto
the wrong IRP5, and nothing in the payslip would look wrong.

**The generator refuses rather than guessing, in three places.** No tax year loaded
for the dates asked about; a calendar whose rule and parameter disagree; or an anchor
date so far in the past that walking forward from it is a sign somebody typed a year
wrong. Each raises with a message naming the pay group.

One interpretation is recorded here rather than taken from the workbook, because the
workbook does not address it: **hourly and daily pay groups take their cadence from
``period_end_rule``**, since "hourly" describes how pay is computed and not how often
it is paid. A week-ending rule gives weekly periods; a month rule gives monthly ones.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass
from decimal import Decimal

from employers.models import PayGroup
from payroll.models import PayPeriod
from statutory.models import TaxYear

#: Walking forward from the anchor is bounded. Five thousand periods is roughly a
#: century of weekly pay - past that, the anchor is a typo rather than a calendar.
MAX_PERIODS_WALKED = 5000


class PeriodGenerationError(Exception):
    """The calendar cannot produce periods. Nothing was written."""


@dataclass(frozen=True)
class GeneratedPeriod:
    start: datetime.date
    end: datetime.date
    payment_date: datetime.date


def _last_day_of_month(year: int, month: int) -> datetime.date:
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def _add_months(day: datetime.date, months: int) -> datetime.date:
    """Move a date by whole months, clamping to the month's length."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _cadence(pay_group: PayGroup) -> str:
    """Weekly, fortnightly or monthly — the rhythm periods actually repeat on.

    For the three salaried frequencies this is the frequency itself. For hourly and
    daily it comes from the period end rule, because those words describe how pay is
    computed rather than how often it is paid: an hourly cleaner paid every Friday is
    on a weekly cadence.
    """
    frequency = pay_group.pay_frequency
    if frequency in {
        PayGroup.PayFrequency.WEEKLY,
        PayGroup.PayFrequency.FORTNIGHTLY,
        PayGroup.PayFrequency.MONTHLY,
    }:
        return frequency
    if pay_group.period_end_rule == PayGroup.PeriodEndRule.WEEK_ENDING_DAY:
        return PayGroup.PayFrequency.WEEKLY
    return PayGroup.PayFrequency.MONTHLY


def _first_period_end(pay_group: PayGroup) -> datetime.date:
    """The end of the first period, on or after the anchor."""
    start = pay_group.first_period_start
    rule = pay_group.period_end_rule

    if rule == PayGroup.PeriodEndRule.WEEK_ENDING_DAY:
        if pay_group.week_ending_weekday is None:
            raise PeriodGenerationError(
                f"{pay_group}: the period end rule is a weekday but no weekday is set."
            )
        ahead = (pay_group.week_ending_weekday - start.weekday()) % 7
        first = start + datetime.timedelta(days=ahead)
        if _cadence(pay_group) == PayGroup.PayFrequency.FORTNIGHTLY:
            first += datetime.timedelta(days=7)
        return first

    if rule == PayGroup.PeriodEndRule.FIXED_DAY_OF_MONTH:
        if pay_group.period_end_day_of_month is None:
            raise PeriodGenerationError(
                f"{pay_group}: the period end rule is a fixed day but no day is set."
            )
        day = pay_group.period_end_day_of_month
        candidate = datetime.date(start.year, start.month, day)
        if candidate < start:
            candidate = _add_months(candidate, 1)
        return candidate

    candidate = _last_day_of_month(start.year, start.month)
    if candidate < start:
        candidate = _last_day_of_month(*_add_months(start, 1).timetuple()[:2])
    return candidate


def _next_end(pay_group: PayGroup, previous_end: datetime.date) -> datetime.date:
    cadence = _cadence(pay_group)
    if cadence == PayGroup.PayFrequency.WEEKLY:
        return previous_end + datetime.timedelta(days=7)
    if cadence == PayGroup.PayFrequency.FORTNIGHTLY:
        return previous_end + datetime.timedelta(days=14)

    if pay_group.period_end_rule == PayGroup.PeriodEndRule.CALENDAR_MONTH_END:
        following = previous_end + datetime.timedelta(days=1)
        return _last_day_of_month(following.year, following.month)
    return _add_months(previous_end, 1)


def working_days_between(
    start: datetime.date, end: datetime.date, days_per_week: Decimal
) -> Decimal:
    """Days in the period that match the employer's weekly pattern.

    Five days means Monday to Friday, six means Monday to Saturday, seven means every
    day — the ordinary readings. Anything else is pro-rated across the calendar days,
    because a four-and-a-half day week has no fixed shape and pretending it does would
    put a specific weekday in the answer that the employer never named.

    Public holidays are deliberately NOT subtracted. A public holiday falling on an
    ordinary working day is paid, so it remains a day available for pay purposes;
    subtracting it here would under-count the month for a salaried employee.
    """
    total_days = (end - start).days + 1
    whole = int(days_per_week)

    if days_per_week == whole and whole in {5, 6, 7}:
        allowed = set(range(whole)) if whole < 7 else set(range(7))
        return Decimal(
            sum(
                1
                for offset in range(total_days)
                if (start + datetime.timedelta(days=offset)).weekday() in allowed
            )
        )

    # Seven is an int, not a Decimal: days in a week is a calendar fact, and the
    # no-hard-coded-rate guard flags Decimal literals precisely because those are the
    # shape a statutory figure takes. Dividing a Decimal by an int keeps the Decimal.
    return (Decimal(total_days) * days_per_week / 7).quantize(Decimal("0.001"))


def periods_for(pay_group: PayGroup, tax_year: TaxYear) -> list[GeneratedPeriod]:
    """Every period of this pay group whose PAYMENT DATE falls inside the tax year.

    Pure: computes and returns, touches no database. That makes the calendar testable
    against a worked example without a tenant, a transaction or a fixture.
    """
    offset = datetime.timedelta(days=pay_group.payment_day_offset)
    start = pay_group.first_period_start
    end = _first_period_end(pay_group)

    if end < start:
        raise PeriodGenerationError(f"{pay_group}: the first period ends before it starts.")

    generated: list[GeneratedPeriod] = []
    walked = 0

    while walked < MAX_PERIODS_WALKED:
        walked += 1
        payment_date = end + offset

        if payment_date > tax_year.end_date:
            break
        if payment_date >= tax_year.start_date:
            generated.append(GeneratedPeriod(start=start, end=end, payment_date=payment_date))

        start = end + datetime.timedelta(days=1)
        end = _next_end(pay_group, end)

    if walked >= MAX_PERIODS_WALKED:
        raise PeriodGenerationError(
            f"{pay_group}: walked {MAX_PERIODS_WALKED} periods from "
            f"{pay_group.first_period_start} without reaching {tax_year.label}. The "
            f"anchor date is almost certainly wrong."
        )

    return generated


def generate_for_tax_year(pay_group: PayGroup, tax_year: TaxYear) -> list[PayPeriod]:
    """Create the tax year's periods for this pay group. Idempotent.

    A period whose start date already exists is left alone rather than duplicated or
    overwritten: re-running after adding a pay group must not renumber or disturb
    periods a payroll run may already have touched.
    """
    computed = periods_for(pay_group, tax_year)
    if not computed:
        raise PeriodGenerationError(
            f"{pay_group}: no period of this calendar pays inside {tax_year.label}. "
            f"Check the anchor date {pay_group.first_period_start} and the payment "
            f"offset of {pay_group.payment_day_offset} days."
        )

    existing = set(
        PayPeriod.objects.filter(pay_group=pay_group).values_list("period_start", flat=True)
    )
    highest = (
        PayPeriod.objects.filter(pay_group=pay_group, tax_year=tax_year)
        .order_by("-period_number")
        .values_list("period_number", flat=True)
        .first()
        or 0
    )

    created = []
    for period in computed:
        if period.start in existing:
            continue
        highest += 1
        created.append(
            PayPeriod.objects.create(
                tenant=pay_group.tenant,
                pay_group=pay_group,
                tax_year=tax_year,
                period_number=highest,
                period_start=period.start,
                period_end=period.end,
                payment_date=period.payment_date,
                working_days_in_period=working_days_between(
                    period.start, period.end, pay_group.default_days_per_week
                ),
            )
        )
    return created


def generate_for_date(pay_group: PayGroup, on_date: datetime.date) -> list[PayPeriod]:
    """Generate the periods of whichever tax year contains a date.

    Refuses when no tax year is loaded, rather than inventing one. A pay period with
    no tax year cannot be reconciled to an IRP5, and the reference data is exactly
    where the answer should come from.
    """
    tax_year = TaxYear.for_date(on_date)
    if tax_year is None:
        raise PeriodGenerationError(
            f"No tax year covers {on_date:%d %B %Y}, so periods cannot be generated. "
            f"Load the statutory reference data for that year first."
        )
    return generate_for_tax_year(pay_group, tax_year)
