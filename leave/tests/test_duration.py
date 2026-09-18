"""Months and days. Never a day count, never weeks (D-204).

Four consecutive months from 15 March ends on 15 July: the employee returns on
the 15th and the last day of leave is the 14th. It is not 120 days, not 17.33
weeks and not four times thirty.

The month-end case is DECIDED, not inherited from whatever the date library
happens to do: four months from 31 October lands on 28 February (29 in a leap
year), the last day of the month, rather than overflowing into 2 or 3 March.
The argument each way is in D-204; what matters here is that it is tested.

No database: this is calendar arithmetic over months and days held as separate
integers, which is how the statute states them.
"""

from __future__ import annotations

import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from leave.duration import last_day_of_period, return_date_after

# --------------------------------------------------------- the worked examples


@pytest.mark.parametrize(
    ("start", "months", "days", "returns", "last_day"),
    [
        # The brief's own example, and the half-open convention this schema uses
        # everywhere: the return date is exclusive, the last day is the day before.
        ((2026, 3, 15), 4, 0, (2026, 7, 15), (2026, 7, 14)),
        # Month-end, CLAMPED: 31 October + 4 months is the last day of February.
        ((2026, 10, 31), 4, 0, (2027, 2, 28), (2027, 2, 27)),
        ((2027, 10, 31), 4, 0, (2028, 2, 29), (2028, 2, 28)),  # leap year
        # 29 February + 4 months is an ordinary corresponding date.
        ((2028, 2, 29), 4, 0, (2028, 6, 29), (2028, 6, 28)),
        # Months first, then days: the ten days of the aggregate.
        ((2026, 3, 16), 4, 10, (2026, 7, 26), (2026, 7, 25)),
        # A start on the 31st with days after it clamps, then adds.
        ((2026, 8, 31), 1, 10, (2026, 10, 10), (2026, 10, 9)),
        ((2026, 3, 16), 0, 10, (2026, 3, 26), (2026, 3, 25)),
    ],
)
def test_the_end_of_a_period_of_months_and_days(start, months, days, returns, last_day):
    assert return_date_after(datetime.date(*start), months=months, days=days) == datetime.date(
        *returns
    )
    assert last_day_of_period(datetime.date(*start), months=months, days=days) == datetime.date(
        *last_day
    )


def test_a_period_of_no_length_returns_the_same_day():
    day = datetime.date(2026, 3, 16)
    assert return_date_after(day, months=0, days=0) == day


def test_a_negative_period_is_refused():
    with pytest.raises(ValueError, match="never negative"):
        return_date_after(datetime.date(2026, 3, 16), months=-1, days=0)


# ------------------------------------------------------------- the property


@settings(max_examples=300, deadline=None)
@given(
    start=st.dates(min_value=datetime.date(2024, 1, 1), max_value=datetime.date(2034, 12, 31)),
    months=st.integers(min_value=0, max_value=12),
    days=st.integers(min_value=0, max_value=31),
)
def test_the_period_is_exactly_n_months_and_m_days_by_calendar_arithmetic(start, months, days):
    end = return_date_after(start, months=months, days=days)

    # Undo the days, and what is left is the month step alone.
    month_step = end - datetime.timedelta(days=days)
    expected_year, expected_month = divmod(start.month - 1 + months, 12)
    expected_year += start.year
    expected_month += 1
    assert (month_step.year, month_step.month) == (expected_year, expected_month), (
        "adding months must never spill into a later month than the calendar one"
    )
    # The day of month is preserved unless the target month is too short, in
    # which case it is that month's last day — never the next month's first.
    import calendar

    longest = calendar.monthrange(expected_year, expected_month)[1]
    assert month_step.day == min(start.day, longest)

    assert end >= start
    assert last_day_of_period(start, months=months, days=days) == end - datetime.timedelta(days=1)


@settings(max_examples=300, deadline=None)
@given(
    start=st.dates(min_value=datetime.date(2024, 1, 1), max_value=datetime.date(2034, 12, 31)),
    first=st.integers(min_value=0, max_value=6),
    second=st.integers(min_value=0, max_value=6),
)
def test_consecutive_sequences_neither_overlap_nor_leave_a_gap(start, first, second):
    """A sequence that begins where the previous one returned is contiguous: no
    day belongs to both, and no day belongs to neither."""
    handover = return_date_after(start, months=first, days=0)
    end = return_date_after(handover, months=second, days=0)

    assert last_day_of_period(start, months=first, days=0) == handover - datetime.timedelta(days=1)
    assert handover <= end
    covered = (handover - start).days + (end - handover).days
    assert covered == (end - start).days, "no overlap, no gap"
