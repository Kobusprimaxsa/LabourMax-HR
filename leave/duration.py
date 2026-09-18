"""A period stated in MONTHS AND DAYS, computed by calendar arithmetic (D-204).

The statute says "four consecutive months", not 120 days and not 17.33 weeks, so
the months and the days are held as separate integers and the end is computed
from the calendar. Four months from 15 March returns the employee on 15 July.

**The half-open convention, the same one the whole schema uses.**
``return_date_after()`` gives the day the employee comes back — exclusive, the
way ``effective_to`` is exclusive — and ``last_day_of_period()`` gives the day
before it. Two names, because "the end" is ambiguous and getting it wrong is a
day of somebody's leave.

**The month-end case is DECIDED, not inherited (D-204): the day CLAMPS to the
end of the month.** Four months from 31 October returns on 28 February (29 in a
leap year), not 2 or 3 March. The corresponding-date rule with clamping is the
ordinary legal computation of a calendar month, and a period described as "four
months" should not end in a fifth calendar month. The argument the other way —
that overflowing to 2 March gives the employee the full count of days and
clamping shortens the period by up to three — is real, and is recorded in the
register rather than hidden here. It only ever arises for a start on the 29th,
30th or 31st.

No ORM, no settings, no ``date.today()``: pure arithmetic over what it is given.
"""

from __future__ import annotations

import calendar
import datetime


def _add_months(start: datetime.date, months: int) -> datetime.date:
    """``start`` plus whole calendar months, clamped to the end of the month."""
    zero_based = start.month - 1 + months
    year = start.year + zero_based // 12
    month = zero_based % 12 + 1
    longest = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(start.day, longest))


def return_date_after(start: datetime.date, *, months: int, days: int) -> datetime.date:
    """The day the employee returns: EXCLUSIVE end of a period of ``months``
    calendar months and then ``days`` calendar days from ``start``.

    Months first, then days, because the statute states them that way — four
    months AND ten days — and adding the days first would change the answer
    whenever the intermediate date crosses a month end.
    """
    if months < 0 or days < 0:
        raise ValueError(f"A leave period is never negative: got {months} months and {days} days.")
    return _add_months(start, months) + datetime.timedelta(days=days)


def last_day_of_period(start: datetime.date, *, months: int, days: int) -> datetime.date:
    """The last day ON leave — the day before the return date."""
    return return_date_after(start, months=months, days=days) - datetime.timedelta(days=1)


def exceeds(start: datetime.date, end: datetime.date, *, months: int, days: int) -> bool:
    """Does a span from ``start`` to ``end`` (both inclusive) run past a period of
    ``months`` months and ``days`` days?"""
    return end > last_day_of_period(start, months=months, days=days)
