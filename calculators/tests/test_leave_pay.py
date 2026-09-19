"""Leave pay: the refusals and the edges the golden file does not reach."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from calculators.leave_pay import (
    AveragingWindow,
    LeavePayRefusedError,
    leave_pay,
)
from calculators.tests.test_leave_pay_golden import (
    DAILY,
    DAYS_PER_WEEK,
    THIRTEEN_WEEKS,
    a_window,
    an_input,
)

# ------------------------------------------------------------- the unit rule


@pytest.mark.parametrize(
    ("days", "hours"),
    [(Decimal("0"), Decimal("0")), (Decimal("5"), Decimal("9"))],
)
def test_leave_must_be_priced_in_exactly_one_unit(days, hours):
    """Neither is nothing to pay; both would need a conversion, and D-164
    forbids one anywhere in this system."""
    with pytest.raises(LeavePayRefusedError, match="exactly one unit"):
        leave_pay(an_input(leave_days=days, leave_hours=hours))


def test_negative_leave_is_refused_rather_than_paid_backwards():
    """A cancellation reverses the ledger row (invariant 4); it never arrives
    here as a negative quantity."""
    with pytest.raises(LeavePayRefusedError, match="cannot be negative"):
        leave_pay(an_input(leave_days=Decimal("-1")))


def test_an_employee_with_no_rate_on_file_is_refused():
    with pytest.raises(LeavePayRefusedError, match="no rate to pay it at"):
        leave_pay(an_input(leave_days=Decimal("5"), daily_rate=Decimal("0")))


def test_an_hourly_balance_with_no_hourly_rate_is_refused_too():
    with pytest.raises(LeavePayRefusedError, match="no rate to pay it at"):
        leave_pay(an_input(leave_hours=Decimal("9"), hourly_rate=Decimal("0")))


# --------------------------------------------------------- the s35(4) window


def test_variable_remuneration_with_no_window_is_refused_not_quietly_flat_rated():
    """Falling back to the contractual rate is exactly what s35(4) exists to
    prevent — the fallback would look right on the payslip and be the wrong
    figure for every commission earner."""
    with pytest.raises(LeavePayRefusedError, match="no averaging window was supplied"):
        leave_pay(an_input(leave_days=Decimal("5"), remuneration_is_variable=True))


def test_an_empty_window_is_refused():
    with pytest.raises(LeavePayRefusedError, match="no remuneration to average"):
        leave_pay(
            an_input(
                leave_days=Decimal("5"),
                remuneration_is_variable=True,
                window=a_window("0", weeks_available=Decimal("0")),
            )
        )


def test_a_window_longer_than_the_act_allows_is_refused():
    """s35(4)(a) names thirteen weeks. Reaching back further is reaching past
    the period the Act names, and it would flatter or depress the average
    depending on what happened to be there."""
    with pytest.raises(LeavePayRefusedError, match="Averaging over longer than the Act allows"):
        leave_pay(
            an_input(
                leave_days=Decimal("5"),
                remuneration_is_variable=True,
                window=a_window("60000", weeks_available=Decimal("20")),
            )
        )


def test_a_window_of_exactly_the_statutory_length_is_allowed():
    """Both sides of the boundary, D-158's standing lesson."""
    result = leave_pay(
        an_input(
            leave_days=Decimal("5"),
            remuneration_is_variable=True,
            window=a_window("39000.00", weeks_available=THIRTEEN_WEEKS.value),
        )
    )

    assert result.amount.exact == Decimal("3000.000000")


def test_negative_remuneration_in_the_window_is_refused():
    with pytest.raises(LeavePayRefusedError, match="not a rate"):
        leave_pay(
            an_input(
                leave_days=Decimal("5"),
                remuneration_is_variable=True,
                window=a_window("-100"),
            )
        )


@pytest.mark.parametrize(
    ("field", "unit", "days", "hours"),
    [
        ("days_per_week", "days", Decimal("5"), Decimal("0")),
        ("hours_per_week", "hours", Decimal("0"), Decimal("45")),
    ],
)
def test_a_weekly_average_with_no_working_pattern_to_divide_by_is_refused(field, unit, days, hours):
    with pytest.raises(LeavePayRefusedError, match="nothing to divide by") as raised:
        leave_pay(
            an_input(
                leave_days=days,
                leave_hours=hours,
                remuneration_is_variable=True,
                window=a_window("39000.00"),
                **{field: Decimal("0")},
            )
        )

    assert f"0 {unit} a week" in str(raised.value)


def test_the_window_carries_its_own_row_key_for_the_trace():
    """``AveragingWindow`` is a multi-column structure over one reference row, so
    it satisfies ``base.Sourced`` directly rather than repeating the key
    (D-215)."""
    window = AveragingWindow(
        weeks=THIRTEEN_WEEKS, weeks_available=Decimal("13"), remuneration=Decimal("39000")
    )

    assert (window.table, window.row_id) == ("statutory_parameter", 41)


# ---------------------------------------------------------------- properties


AMOUNTS = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("200000"), places=2, allow_nan=False
)


@settings(max_examples=200, deadline=None)
@given(
    remuneration=AMOUNTS,
    days=st.decimals(min_value=Decimal("0.5"), max_value=Decimal("21"), places=3),
)
def test_leave_pay_is_never_negative_and_scales_with_the_days_taken(remuneration, days):
    result = leave_pay(
        an_input(
            leave_days=days,
            remuneration_is_variable=True,
            window=a_window(remuneration),
        )
    )
    twice = leave_pay(
        an_input(
            leave_days=days * 2,
            remuneration_is_variable=True,
            window=a_window(remuneration),
        )
    )

    assert result.amount.exact >= Decimal("0")
    assert twice.amount.exact >= result.amount.exact


@settings(max_examples=200, deadline=None)
@given(remuneration=AMOUNTS)
def test_a_full_week_of_leave_pays_one_weeks_average(remuneration):
    """The property s35(4) exists for: however the earnings were distributed
    across the window, a week of leave is worth a week of the average."""
    result = leave_pay(
        an_input(
            leave_days=DAYS_PER_WEEK,
            remuneration_is_variable=True,
            window=a_window(remuneration),
        )
    )

    assert result.amount.exact == result.average_weekly.exact


@settings(max_examples=100, deadline=None)
@given(days=st.decimals(min_value=Decimal("0.5"), max_value=Decimal("21"), places=3))
def test_the_ordinary_path_is_always_the_contractual_rate_times_the_days(days):
    result = leave_pay(an_input(leave_days=days))

    assert result.amount.exact == (days * DAILY).quantize(Decimal("0.000001"))
    assert result.used_the_average is False
