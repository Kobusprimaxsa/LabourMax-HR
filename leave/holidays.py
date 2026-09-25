"""One employer's public holiday calendar - the statutory one, as that employer
has lawfully varied it (D-319).

Everything that decides "is this date a public holiday for this employee" reads
here, so the answer cannot differ between capture, bucketing, the unworked-
holiday count, payroll and leave. It used to: leave read the employer's
observance rows and every pay path read the raw gazette, so an exchanged
holiday was a working day for leave and a holiday for pay, and the substitute
day it was exchanged FOR was a holiday for leave and nothing at all for pay -
an hourly worker went unpaid for it, and working it paid ordinary rates.

Two views, because they are two questions:

* ``pay_holidays`` - the days s18 prices: the gazette, LESS days exchanged away
  under PHA s2(2), PLUS the substitute days they were exchanged for. A day
  worked by agreement under BCEA s18(1) STAYS a holiday and is priced at
  s18(2)(b); nothing here touches that path.
* ``leave_holidays`` - the days leave is not charged on: the pay holidays PLUS
  an employer's own day off the calendar knows nothing about (an ``observed``
  row with no ``public_holiday``), which is a day off but not a public holiday.

Callers pin the employer's tenant.
"""

from __future__ import annotations

import datetime

from statutory import resolve


def _rows(employer, start: datetime.date, end: datetime.date):
    from leave.models import PublicHolidayObservance

    return list(
        PublicHolidayObservance.objects.filter(
            employer=employer, observance_date__gte=start, observance_date__lte=end
        )
    )


def pay_holidays(employer, start: datetime.date, end: datetime.date) -> dict[datetime.date, str]:
    """Every public holiday s18 prices for this employer, date to name."""
    from leave.models import PublicHolidayObservance

    treatment = PublicHolidayObservance.Treatment
    days = {
        holiday.holiday_date: holiday.name
        for holiday in resolve.public_holidays_between(start, end)
    }
    for row in _rows(employer, start, end):
        if row.treatment == treatment.EXCHANGED:
            days.pop(row.observance_date, None)
        elif row.treatment == treatment.SUBSTITUTE:
            days[row.observance_date] = row.name
    return days


def leave_holidays(employer, start: datetime.date, end: datetime.date) -> set[datetime.date]:
    """Every day leave is not charged on for this employer."""
    from leave.models import PublicHolidayObservance

    days = set(pay_holidays(employer, start, end))
    for row in _rows(employer, start, end):
        if (
            row.treatment == PublicHolidayObservance.Treatment.OBSERVED
            and row.public_holiday_id is None
        ):
            days.add(row.observance_date)
    return days


def is_pay_holiday(employer, day: datetime.date) -> bool:
    return day in pay_holidays(employer, day, day)


def pay_holiday_name(employer, day: datetime.date) -> str | None:
    return pay_holidays(employer, day, day).get(day)


def is_leave_holiday(employer, day: datetime.date) -> bool:
    return day in leave_holidays(employer, day, day)


def unpaired_exchange_warnings(
    employer, start: datetime.date | None = None, end: datetime.date | None = None
) -> list[str]:
    """Exchanged holidays with no substitute day recorded (D-319, Finding 3).

    A WARNING, never a refusal: the software does not police the agreement,
    and an unpaired row may be a lawful choice the employer simply has not
    finished recording - or a day that was really worked by agreement and
    mis-recorded as an exchange. The wording names both.
    """
    from leave.models import PublicHolidayObservance

    rows = PublicHolidayObservance.objects.filter(
        employer=employer,
        treatment=PublicHolidayObservance.Treatment.EXCHANGED,
        substitute__isnull=True,
    )
    if start is not None:
        rows = rows.filter(observance_date__gte=start)
    if end is not None:
        rows = rows.filter(observance_date__lte=end)
    return [exchange_warning(row) for row in rows.order_by("observance_date")]


def exchange_warning(row) -> str:
    return (
        f"{row.observance_date:%d %B %Y} ({row.name}) is recorded as EXCHANGED for another "
        f"day under the Public Holidays Act s2(2), but no substitute day is recorded. An "
        f"exchange swaps the holiday FOR another day: until that day is recorded, this "
        f"employee has lost the holiday and not received its replacement, and it is paid "
        f"as an ordinary day. If the employee instead WORKED the holiday by agreement under "
        f"BCEA s18(1), record it as worked by agreement - it stays a public holiday and "
        f"s18(2)(b) pays at least double."
    )
