"""Termination payout: the refusals and the edges the golden file does not reach."""

from __future__ import annotations

from decimal import Decimal

import pytest

from calculators.remuneration import RemunerationRefusedError
from calculators.termination import (
    ProRataLeaveRule,
    TerminationRefusedError,
    termination_payout,
)
from calculators.tests.test_termination_golden import (
    FOUR_WEEKS_NOTICE,
    SEVERANCE,
    WEEKLY,
    amount_of,
    an_input,
)


def test_the_termination_refusal_is_catchable_as_the_section_35_one():
    assert issubclass(TerminationRefusedError, RemunerationRefusedError)


# --------------------------------------------------------------- s38 refusals


def test_paying_in_lieu_with_no_band_is_refused():
    """s38(1) pays the remuneration for "the notice period", and the period is
    the band. There is nothing here to assume."""
    with pytest.raises(TerminationRefusedError, match="no notice band was supplied"):
        termination_payout(an_input(notice_is_paid_in_lieu=True))


def test_paying_in_lieu_with_no_rate_is_refused():
    with pytest.raises(TerminationRefusedError, match="no rate to pay it at"):
        termination_payout(
            an_input(
                notice_is_paid_in_lieu=True,
                notice_band=FOUR_WEEKS_NOTICE,
                weekly_rate=Decimal("0"),
            )
        )


def test_an_accommodation_offset_larger_than_the_notice_pay_floors_at_nil():
    """s39(2) reduces what is owed; it does not turn a payment into a debt."""
    result = termination_payout(
        an_input(
            notice_is_paid_in_lieu=True,
            notice_band=FOUR_WEEKS_NOTICE,
            accommodation_offset=Decimal("20000.00"),
        )
    )

    assert result.notice_pay.exact == Decimal("0")
    assert any("does not create a debt" in w for w in result.trace.warnings)


# ------------------------------------------------------------ s40(b) and s40(c)


def test_leave_due_in_both_units_at_once_is_refused():
    with pytest.raises(TerminationRefusedError, match="exactly one unit"):
        termination_payout(an_input(leave_due_days=Decimal("5"), leave_due_hours=Decimal("9")))


def test_an_hours_denominated_leave_balance_is_paid_at_the_hourly_rate():
    result = termination_payout(an_input(leave_due_hours=Decimal("45.000")))

    assert result.leave_due_pay.exact == Decimal("3000.000015")
    assert amount_of(result, "LEAVE_PAY") == Decimal("3000.000015")


def test_the_pro_rata_qualifying_period_must_be_supplied():
    with pytest.raises(TerminationRefusedError, match="qualifying period was not supplied"):
        termination_payout(
            an_input(
                pro_rata_minimum_service_months=None,
                months_of_service=Decimal("8"),
                incomplete_cycle_days=Decimal("3"),
            )
        )


def test_the_seventeen_day_ratio_must_be_supplied_to_check_the_floor():
    with pytest.raises(TerminationRefusedError, match="ratio was not supplied"):
        termination_payout(
            an_input(
                pro_rata_rule=None,
                months_of_service=Decimal("8"),
                incomplete_cycle_days=Decimal("3"),
                days_worked_in_incomplete_cycle=Decimal("85"),
            )
        )


def test_a_ratio_of_zero_is_refused_rather_than_divided_by():
    with pytest.raises(TerminationRefusedError, match="Nothing can be divided by that"):
        termination_payout(
            an_input(
                pro_rata_rule=ProRataLeaveRule(
                    days_worked_per_leave_day=Decimal("0"),
                    table="leave_rule_set",
                    row_id=81,
                ),
                months_of_service=Decimal("8"),
                incomplete_cycle_days=Decimal("3"),
                days_worked_in_incomplete_cycle=Decimal("85"),
            )
        )


def test_an_hours_denominated_incomplete_cycle_pays_the_ledger_and_says_the_floor_was_not_checked():
    """s40(c)(i)'s floor is stated in DAYS. An hours cycle cannot be compared
    against it without a conversion D-164 forbids, so the ledger is paid and the
    gap is named rather than quietly ignored."""
    result = termination_payout(
        an_input(
            months_of_service=Decimal("8"),
            incomplete_cycle_hours=Decimal("18.000"),
            days_worked_in_incomplete_cycle=Decimal("85"),
        )
    )

    assert result.pro_rata_leave_pay.exact == Decimal("1200.000006")
    assert any("cannot be compared" in w for w in result.trace.warnings)


def test_days_worked_with_nothing_accrued_is_flagged_rather_than_passed_over():
    """A cycle the engine never accrued into is either a new employee or a
    defect, and s40(c) is still owed either way once the qualifying period is
    past."""
    result = termination_payout(
        an_input(
            months_of_service=Decimal("8"),
            days_worked_in_incomplete_cycle=Decimal("85"),
        )
    )

    assert any("the ledger accrued nothing" in w for w in result.trace.warnings)
    assert result.pro_rata_leave_pay.exact == Decimal("3000.000000"), "the floor is still paid"


# --------------------------------------------------------------- s41 refusals


def test_an_operational_dismissal_with_no_severance_rule_is_refused():
    """s41(3) lets the Minister vary the multiplier by notice in the Gazette, so
    it is never a literal — and an operational dismissal priced without it would
    silently pay nothing."""
    with pytest.raises(TerminationRefusedError, match="no severance rule was supplied"):
        termination_payout(
            an_input(
                dismissed_for_operational_requirements=True,
                completed_years_of_service=Decimal("7"),
            )
        )


def test_severance_uses_the_rule_sets_own_multiplier_not_a_literal_week():
    from calculators.termination import SeveranceRule

    generous = SeveranceRule(
        weeks_per_completed_year=Decimal("2.00"),
        requires_operational_reason=True,
        table="termination_rule_set",
        row_id=72,
    )

    result = termination_payout(
        an_input(
            dismissed_for_operational_requirements=True,
            severance_rule=generous,
            completed_years_of_service=Decimal("3"),
        )
    )

    assert result.severance.exact == Decimal("6") * WEEKLY


# -------------------------------------------------------------------- the trace


def test_every_reference_row_the_payout_read_is_in_the_trace():
    result = termination_payout(
        an_input(
            notice_is_paid_in_lieu=True,
            notice_band=FOUR_WEEKS_NOTICE,
            months_of_service=Decimal("8"),
            incomplete_cycle_days=Decimal("3.000"),
            days_worked_in_incomplete_cycle=Decimal("85"),
            dismissed_for_operational_requirements=True,
            severance_rule=SEVERANCE,
            completed_years_of_service=Decimal("2"),
        )
    )

    assert set(result.trace.statutory_rows) == {
        ("termination_notice_band", 61),
        ("statutory_parameter", 51),
        ("leave_rule_set", 81),
        ("termination_rule_set", 71),
    }
    assert result.trace.calculator == "termination.termination_payout"


def test_a_payout_that_reads_no_notice_band_does_not_record_one():
    """Provenance is what was actually read. A band resolved and then not used —
    because the employee worked their notice — is not evidence of anything."""
    result = termination_payout(
        an_input(notice_band=FOUR_WEEKS_NOTICE, leave_due_days=Decimal("5"))
    )

    assert ("termination_notice_band", 61) not in result.trace.statutory_rows


def test_the_whole_payout_adds_up_to_its_own_lines():
    result = termination_payout(
        an_input(
            notice_is_paid_in_lieu=True,
            notice_band=FOUR_WEEKS_NOTICE,
            leave_due_days=Decimal("10.000"),
            months_of_service=Decimal("18"),
            incomplete_cycle_days=Decimal("4.000"),
            days_worked_in_incomplete_cycle=Decimal("68"),
            dismissed_for_operational_requirements=True,
            severance_rule=SEVERANCE,
            completed_years_of_service=Decimal("1"),
        )
    )

    assert result.total.exact == sum(line.amount.exact for line in result.lines)
    assert result.total.exact == (
        result.notice_pay.exact
        + result.leave_due_pay.exact
        + result.pro_rata_leave_pay.exact
        + result.severance.exact
    )
    assert [line.component_code for line in result.lines] == [
        "NOTICE_PAY",
        "LEAVE_PAY",
        "LEAVE_PAY",
        "SEVERANCE",
    ]
