"""Leave pay against the Act's own words — BCEA s21 and s35.

No regulator publishes a worked leave pay example, so the sections themselves
are the golden source, transcribed here beside the test that holds the code to
them. That is D-150's position, already restated for premium pay in
``test_gross_statute.py``.

Every quotation is from the Basic Conditions of Employment Act 75 of 1997 as
published in Government Gazette 18491 of 5 December 1997, except the s35(5)
determination, which is Government Notice 691 in Government Gazette 24889 of
23 May 2003, effective 1 July 2003.

**Marked ``statute``, not ``golden``** (D-283). These reproduce what a section's
own words dictate, not a figure a regulator published, so they are not part of
the golden set behind ``--golden-tests-passed`` — they run with every other test.
Until 24 September 2026 they carried the ``golden`` mark and the file said
"_golden", which made the flag's meaning depend on which files one counted.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.leave_pay import (
    AveragingWindow,
    LeavePayInput,
    leave_pay,
)

pytestmark = pytest.mark.statute

MARCH = datetime.date(2026, 3, 31)

#: s35(4)(a)'s window, as ``VARIABLE_EARNINGS_AVERAGE_WEEKS`` loads it.
THIRTEEN_WEEKS = StatutoryFigure(
    value=Decimal("13.000000"),
    table="statutory_parameter",
    row_id=41,
    description="BCEA s35(4)(a) averaging window",
)

#: A five-day, 45-hour week at R600 a day — R3 000 a week, R66.666667 an hour.
DAILY = Decimal("600.00")
HOURLY = Decimal("66.666667")
DAYS_PER_WEEK = Decimal("5")
HOURS_PER_WEEK = Decimal("45")


def an_input(**overrides) -> LeavePayInput:
    values = {
        "calculated_for": MARCH,
        "leave_days": Decimal("0"),
        "leave_hours": Decimal("0"),
        "daily_rate": DAILY,
        "hourly_rate": HOURLY,
        "days_per_week": DAYS_PER_WEEK,
        "hours_per_week": HOURS_PER_WEEK,
    }
    values.update(overrides)
    return LeavePayInput(**values)


def a_window(remuneration, weeks_available=Decimal("13")) -> AveragingWindow:
    return AveragingWindow(
        weeks=THIRTEEN_WEEKS,
        weeks_available=weeks_available,
        remuneration=Decimal(remuneration),
    )


# ----------------------------------------------------- s21: the ordinary case


def test_leave_pay_is_what_the_employee_would_have_earned_by_working_it():
    """s21(1): "An employer must pay an employee leave pay at least equivalent to
    the remuneration that the employee would have received for working for a
    period equal to the period of annual leave, calculated — (a) at the
    employee's rate of remuneration immediately before the beginning of the
    period of annual leave; and (b) in accordance with section 35."

    Five days of leave for an employee on R600 a day is five days' pay. The
    whole of s21 for an employee paid by time whose pay does not swing.
    """
    result = leave_pay(an_input(leave_days=Decimal("5.000")))

    assert result.amount.exact == Decimal("3000.000000")
    assert result.rate_used.exact == DAILY
    assert result.used_the_average is False
    assert result.line.component_code == "LEAVE_PAY"


def test_a_part_day_of_leave_is_a_part_day_of_pay():
    """P6 posts half days as 0.500 (D-171), and a day's pay follows the day."""
    result = leave_pay(an_input(leave_days=Decimal("0.500")))

    assert result.amount.exact == Decimal("300.000000")


def test_an_hours_balance_is_priced_at_the_hourly_rate_and_never_converted():
    """D-164: a balance is held in whatever unit its own accrual produced, and
    nothing in this system converts between days and hours. An employee who
    accrues by the hour is paid by the hour."""
    result = leave_pay(an_input(leave_hours=Decimal("9.000")))

    assert result.amount.exact == Decimal("600.000003"), "nine hours, at the derived rate"
    assert result.line.units == Decimal("9.000")


# ------------------------------------------- s35(4): the variable-earnings average


def test_the_average_is_the_preceding_thirteen_weeks_remuneration_over_thirteen():
    """s35(4): "If an employee's remuneration or wage is calculated, either
    wholly or in part, on a basis other than time or if an employee's
    remuneration or wage fluctuates significantly from period to period, any
    payment to that employee in terms of this Act must be calculated by
    reference to the employee's remuneration or wage during — (a) the preceding
    13 weeks".

    R39 000 over the thirteen weeks is R3 000 a week; on a five-day week that is
    R600 a day, and five days of leave is R3 000 — one week of leave paying one
    week's average earnings, which is the property the whole section exists for.
    """
    result = leave_pay(
        an_input(
            leave_days=Decimal("5.000"),
            remuneration_is_variable=True,
            window=a_window("39000.00"),
        )
    )

    assert result.average_weekly.exact == Decimal("3000.000000")
    assert result.rate_used.exact == Decimal("600.000000")
    assert result.amount.exact == Decimal("3000.000000")
    assert result.used_the_average is True


def test_a_shorter_employment_is_averaged_over_that_shorter_period():
    """s35(4)(b): "if the employee has been in employment for a shorter period,
    that period."

    Six weeks' employment and R21 000 earned in it averages R3 500 a week — not
    R21 000 over thirteen, which would price a new employee's leave at well
    under half of what they have been earning.
    """
    result = leave_pay(
        an_input(
            leave_days=Decimal("5.000"),
            remuneration_is_variable=True,
            window=a_window("21000.00", weeks_available=Decimal("6")),
        )
    )

    assert result.average_weekly.exact == Decimal("3500.000000")
    assert Decimal("21000.00") / Decimal("13") < Decimal("1616"), "the wrong divisor"
    assert result.amount.exact == Decimal("3500.000000")


def test_the_average_prices_an_hours_balance_off_the_same_weekly_figure():
    """Weekly is the hub (D-106): the daily rate is the weekly average over the
    days worked in a week, and the hourly rate the same average over the hours.
    Deriving one from the other instead is how an employee comes to be paid one
    figure for a day of leave and another for a day of work."""
    result = leave_pay(
        an_input(
            leave_hours=Decimal("45.000"),
            remuneration_is_variable=True,
            window=a_window("39000.00"),
        )
    )

    assert result.rate_used.exact == Decimal("66.666667"), "3 000 over 45 hours"
    assert result.amount.exact == Decimal("3000.000000"), "a full week of hours"
    assert result.trace.warnings == (), "the average equals the contractual rate here"


def test_the_average_is_used_even_where_it_comes_out_below_the_contractual_rate():
    """s21(1) says leave pay must be "at least equivalent to" the remuneration
    calculated "(a) at the employee's rate ... and (b) in accordance with
    section 35". The two limbs are cumulative — (b) governs how (a)'s rate is
    found — so this is NOT the greater of the flat rate and the average.

    Paying the greater would invent an entitlement the Act does not give. The
    result says so in a warning instead, because a commission earner whose last
    quarter collapsed is a payslip somebody should look at.
    """
    result = leave_pay(
        an_input(
            leave_days=Decimal("5.000"),
            remuneration_is_variable=True,
            window=a_window("19500.00"),
        )
    )

    assert result.rate_used.exact == Decimal("300.000000")
    assert result.rate_used.exact < DAILY
    assert result.amount.exact == Decimal("1500.000000")
    assert any("below the contractual rate" in w for w in result.trace.warnings)


def test_the_window_is_a_statutory_row_and_the_trace_records_it():
    """Thirteen is ``VARIABLE_EARNINGS_AVERAGE_WEEKS`` in ``statutory_parameter``
    with its citation, never a 13 written in the calculator."""
    result = leave_pay(
        an_input(
            leave_days=Decimal("5.000"),
            remuneration_is_variable=True,
            window=a_window("39000.00"),
        )
    )

    assert result.trace.statutory_rows == (("statutory_parameter", 41),)
    assert result.trace.inputs["window_weeks"] == "13.000000"


def test_the_ordinary_path_reads_no_statutory_row_at_all():
    """An employee paid by time whose pay does not swing is priced at their own
    contractual rate. That is their contract, not a gazetted figure, so there is
    no reference row to record — and an empty list is the honest answer rather
    than a row that was never read."""
    result = leave_pay(an_input(leave_days=Decimal("5.000")))

    assert result.trace.statutory_rows == ()
