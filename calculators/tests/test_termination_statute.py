"""Termination payout against the Act's own words — BCEA ss 38, 39, 40, 41.

No regulator publishes a worked termination example, so the sections themselves
are the golden source, transcribed beside the tests that hold the code to them
(D-150's position, already restated for premium pay and for leave pay).

Quotations are from the Basic Conditions of Employment Act 75 of 1997 as
published in Government Gazette 18491 of 5 December 1997, and from Sectoral
Determination 1: Contract Cleaning Sector, clause 23.

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
from calculators.remuneration import AveragingWindow
from calculators.termination import (
    NoticeBand,
    NoticeUnit,
    ProRataLeaveRule,
    SeveranceRule,
    TerminationInput,
    termination_payout,
)

pytestmark = pytest.mark.statute

MARCH = datetime.date(2026, 3, 31)

#: A five-day, 45-hour week at R600 a day — R3 000 a week, R66.666667 an hour.
WEEKLY = Decimal("3000.00")
DAILY = Decimal("600.00")
HOURLY = Decimal("66.666667")

FOUR_MONTHS = StatutoryFigure(
    value=Decimal("4.000000"),
    table="statutory_parameter",
    row_id=51,
    description="BCEA s40(c) qualifying period",
)
THIRTEEN_WEEKS = StatutoryFigure(value=Decimal("13.000000"), table="statutory_parameter", row_id=41)

#: BCEA's own third band: four weeks from one year's service.
FOUR_WEEKS_NOTICE = NoticeBand(
    notice_value=Decimal("4.00"),
    notice_unit=NoticeUnit.WEEKS,
    table="termination_notice_band",
    row_id=61,
)
#: SD1 clause 23(1)(a): one working day during the first four weeks.
ONE_DAY_NOTICE = NoticeBand(
    notice_value=Decimal("1.00"),
    notice_unit=NoticeUnit.DAYS,
    table="termination_notice_band",
    row_id=62,
)

SEVERANCE = SeveranceRule(
    weeks_per_completed_year=Decimal("1.00"),
    requires_operational_reason=True,
    table="termination_rule_set",
    row_id=71,
)
#: s40(c)(i)'s 17, which is leave_rule_set.annual_accrual_ratio_days_worked.
SEVENTEEN = ProRataLeaveRule(
    days_worked_per_leave_day=Decimal("17"), table="leave_rule_set", row_id=81
)


def an_input(**overrides) -> TerminationInput:
    values = {
        "calculated_for": MARCH,
        "weekly_rate": WEEKLY,
        "daily_rate": DAILY,
        "hourly_rate": HOURLY,
        "days_per_week": Decimal("5"),
        "hours_per_week": Decimal("45"),
        "pro_rata_minimum_service_months": FOUR_MONTHS,
        "pro_rata_rule": SEVENTEEN,
    }
    values.update(overrides)
    return TerminationInput(**values)


def amount_of(result, code) -> Decimal:
    return sum(
        (line.amount.exact for line in result.lines if line.component_code == code),
        Decimal("0"),
    )


# ------------------------------------------------- s38: payment instead of notice


def test_pay_in_lieu_is_what_the_notice_period_would_have_earned():
    """s38(1): "Instead of giving an employee notice in terms of section 37, an
    employer may pay the employee the remuneration the employee would have
    received, calculated in accordance with section 35, if the employee had
    worked during the notice period."

    Four weeks at R3 000 a week is R12 000 — the period, at the rate.
    """
    result = termination_payout(
        an_input(notice_is_paid_in_lieu=True, notice_band=FOUR_WEEKS_NOTICE)
    )

    assert result.notice_pay.exact == Decimal("12000.000000")
    assert amount_of(result, "NOTICE_PAY") == Decimal("12000.000000")


def test_a_band_stated_in_working_days_is_paid_in_days():
    """SD1 clause 23(1)(a) gives "not less than one working day's" notice during
    the first four weeks, and 23(1)(d)(i) prices it at "the daily wage the
    employee is receiving at the time of such termination".

    The band carries its own unit (D-68) because SD1's shortest period is a DAY
    where every BCEA band is weeks. Reading it as a week overpays sevenfold.
    """
    result = termination_payout(an_input(notice_is_paid_in_lieu=True, notice_band=ONE_DAY_NOTICE))

    assert result.notice_pay.exact == DAILY
    assert result.notice_pay.exact == Decimal("600.000000")


def test_the_sd1_pay_in_lieu_floor_is_lower_than_s38_so_the_two_do_not_conflict():
    """SD1 clause 23(1)(d): "an employee or employer may terminate the contract
    without notice by paying ... in lieu of such notice NOT LESS THAN in the case
    of — (i) one working day's notice, the daily wage ...; (ii) four weeks'
    notice, DOUBLE THE WEEKLY WAGE the employee is receiving at the time of such
    termination."

    The determination's figures are a FLOOR, and they attach to the two bands it
    actually has. For the four-week band that floor is two weekly wages, where
    s38(1) pays four — so paying s38 satisfies SD1 as well, and there is nothing
    to reconcile. The one-working-day band agrees exactly.

    This corrects the reading recorded against O-06, which had the determination
    giving a figure "for two weeks" and no band to match it. "Double the weekly
    wage" is the figure; four weeks' notice is the band (D-224).
    """
    sd1_floor_for_four_weeks = Decimal("2") * WEEKLY
    sd1_figure_for_one_day = DAILY

    four_weeks = termination_payout(
        an_input(notice_is_paid_in_lieu=True, notice_band=FOUR_WEEKS_NOTICE)
    )
    one_day = termination_payout(an_input(notice_is_paid_in_lieu=True, notice_band=ONE_DAY_NOTICE))

    assert four_weeks.notice_pay.exact > sd1_floor_for_four_weeks
    assert one_day.notice_pay.exact == sd1_figure_for_one_day


def test_notice_worked_rather_than_paid_produces_no_termination_line():
    """An employee who works the notice period is paid for it on the ordinary
    payslip, as ordinary remuneration. s38 is the alternative to that, not an
    addition to it, and paying both is paying the notice period twice."""
    result = termination_payout(an_input(notice_band=FOUR_WEEKS_NOTICE))

    assert result.notice_pay.exact == Decimal("0")
    assert result.lines == ()


def test_accommodation_the_employee_stays_in_reduces_the_notice_payment():
    """s39(2): "If an employee elects to remain in accommodation ... after the
    employer has terminated the employee's contract of employment in terms of
    section 38, the remuneration that the employer is required to pay in terms
    of section 38 is reduced by that portion of the remuneration that represents
    the agreed value of the accommodation for the period that the employee
    remains in the accommodation."

    A reduction of the s38 payment and of nothing else.
    """
    result = termination_payout(
        an_input(
            notice_is_paid_in_lieu=True,
            notice_band=FOUR_WEEKS_NOTICE,
            accommodation_offset=Decimal("1500.00"),
        )
    )

    assert result.notice_pay.exact == Decimal("10500.000000")
    assert any("s39(2)" in w for w in result.trace.warnings)


# ------------------------------------------------- s40: payments on termination


def test_leave_due_and_not_taken_is_paid_at_the_section_21_rate():
    """s40(b): "remuneration calculated in accordance with section 21(1) for any
    period of annual leave due in terms of section 20(2) that the employee has
    not taken"."""
    result = termination_payout(an_input(leave_due_days=Decimal("7.500")))

    assert result.leave_due_pay.exact == Decimal("4500.000000")


def test_the_incomplete_cycle_pays_the_ledgers_own_accrual_when_it_is_the_greater():
    """s40(c)(ii): "remuneration calculated on any basis that is at least as
    favourable to the employee as that calculated in terms of subparagraph (i)."

    The employee's own accrual basis — P6's engine, running the rule set's
    straight-line monthly method — produced 5 days for the incomplete cycle. The
    s40(c)(i) floor on 68 days worked is 4. The more favourable basis wins.
    """
    result = termination_payout(
        an_input(
            months_of_service=Decimal("8"),
            incomplete_cycle_days=Decimal("5.000"),
            days_worked_in_incomplete_cycle=Decimal("68"),
        )
    )

    assert Decimal("68") / Decimal("17") == Decimal("4"), "the s40(c)(i) floor"
    assert result.pro_rata_leave_pay.exact == Decimal("3000.000000"), "five days, not four"


def test_the_seventeen_day_ratio_is_a_floor_and_is_paid_when_the_ledger_accrued_less():
    """s40(c)(i): "one day's remuneration in respect of every 17 days on which
    the employee worked or was entitled to be paid".

    Subparagraph (ii) lets the employer use a more favourable basis, so (i) is a
    minimum rather than the answer. An employer whose rule set accrues less
    generously than the Act's own ratio underpays every leaver, and the result
    says so rather than quietly paying the ledger.
    """
    result = termination_payout(
        an_input(
            months_of_service=Decimal("8"),
            incomplete_cycle_days=Decimal("3.000"),
            days_worked_in_incomplete_cycle=Decimal("85"),
        )
    )

    assert Decimal("85") / Decimal("17") == Decimal("5"), "the s40(c)(i) floor"
    assert result.pro_rata_leave_pay.exact == Decimal("3000.000000"), "five days, not three"
    assert any("floor" in w for w in result.trace.warnings)


def test_four_months_exactly_does_not_qualify_because_the_act_says_longer_than():
    """s40(c) opens "if the employee has been in employment longer than four
    months". Longer than, so the boundary sits with the lower band — tested from
    both sides, which is D-158's standing lesson about exactly this shape."""
    at_four = termination_payout(
        an_input(
            months_of_service=Decimal("4"),
            incomplete_cycle_days=Decimal("3.000"),
            days_worked_in_incomplete_cycle=Decimal("85"),
        )
    )
    just_over = termination_payout(
        an_input(
            months_of_service=Decimal("4.1"),
            incomplete_cycle_days=Decimal("3.000"),
            days_worked_in_incomplete_cycle=Decimal("85"),
        )
    )

    assert at_four.pro_rata_leave_pay.exact == Decimal("0")
    assert just_over.pro_rata_leave_pay.exact == Decimal("3000.000000")


def test_leave_due_and_the_incomplete_cycle_are_two_separate_amounts():
    """s40(b) is leave from cycles that COMPLETED and s40(c) is the cycle still
    running. Paying one and calling it both is the commonest way a leaver is
    short-paid, because the completed-cycle balance is the visible one."""
    result = termination_payout(
        an_input(
            leave_due_days=Decimal("15.000"),
            months_of_service=Decimal("18"),
            incomplete_cycle_days=Decimal("5.000"),
            days_worked_in_incomplete_cycle=Decimal("68"),
        )
    )

    assert result.leave_due_pay.exact == Decimal("9000.000000")
    assert result.pro_rata_leave_pay.exact == Decimal("3000.000000")
    assert result.total.exact == Decimal("12000.000000")


# --------------------------------------------------------------- s41: severance


def test_severance_is_a_weeks_remuneration_for_each_completed_year():
    """s41(2): "An employer must pay an employee who is dismissed for reasons
    based on the employer's operational requirements severance pay equal to at
    least one week's remuneration for each completed year of continuous service
    with that employer, calculated in accordance with section 35."

    Seven completed years at one week each is seven weekly wages.
    """
    result = termination_payout(
        an_input(
            dismissed_for_operational_requirements=True,
            severance_rule=SEVERANCE,
            completed_years_of_service=Decimal("7"),
        )
    )

    assert result.severance.exact == Decimal("21000.000000")


def test_a_partial_year_counts_for_nothing():
    """ "each COMPLETED year of continuous service" — eleven months is not a
    year, and rounding it up is inventing an entitlement."""
    result = termination_payout(
        an_input(
            dismissed_for_operational_requirements=True,
            severance_rule=SEVERANCE,
            completed_years_of_service=Decimal("0"),
        )
    )

    assert result.severance.exact == Decimal("0")
    assert any("no COMPLETED year" in w for w in result.trace.warnings)


def test_a_resignation_attracts_no_severance_at_all():
    """s41(2) is conditioned on dismissal "for reasons based on the employer's
    operational requirements" — s41(1)'s "economic, technological, structural or
    similar needs". A resignation is none of those."""
    result = termination_payout(
        an_input(severance_rule=SEVERANCE, completed_years_of_service=Decimal("7"))
    )

    assert result.severance.exact == Decimal("0")
    assert result.lines == ()


def test_unreasonably_refusing_alternative_employment_forfeits_severance_and_says_why():
    """s41(4): "An employee who unreasonably refuses to accept the employer's
    offer of alternative employment with that employer or any other employer, is
    not entitled to severance pay in terms of subsection (2)."

    "Unreasonably" is a judgement the employer makes and must be able to
    justify, so the reason is recorded rather than a zero appearing unexplained.
    """
    result = termination_payout(
        an_input(
            dismissed_for_operational_requirements=True,
            severance_rule=SEVERANCE,
            completed_years_of_service=Decimal("7"),
            unreasonably_refused_alternative_employment=True,
        )
    )

    assert result.severance.exact == Decimal("0")
    assert any("s41(4)" in w for w in result.trace.warnings)


# ------------------------------------------ s35 governs all three payments


def test_a_variable_earner_has_every_payment_priced_off_the_same_average():
    """s35(5) names s21, s38 and s41 together, so one average serves all three.
    R19 500 over the thirteen weeks is R1 500 a week and R300 a day — half the
    contractual rate — and notice, leave and severance all move together."""
    result = termination_payout(
        an_input(
            remuneration_is_variable=True,
            window=AveragingWindow(
                weeks=THIRTEEN_WEEKS,
                weeks_available=Decimal("13"),
                remuneration=Decimal("19500.00"),
            ),
            notice_is_paid_in_lieu=True,
            notice_band=FOUR_WEEKS_NOTICE,
            leave_due_days=Decimal("10.000"),
            dismissed_for_operational_requirements=True,
            severance_rule=SEVERANCE,
            completed_years_of_service=Decimal("3"),
        )
    )

    assert result.rates.weekly == Decimal("1500")
    assert result.notice_pay.exact == Decimal("6000.000000"), "four weeks at the average"
    assert result.leave_due_pay.exact == Decimal("3000.000000"), "ten days at R300"
    assert result.severance.exact == Decimal("4500.000000"), "three weeks at the average"
    assert result.total.exact == Decimal("13500.000000")


def test_an_overdrawn_leave_balance_is_surfaced_and_never_netted_off():
    """D-185, recorded against P7 when the query that finds these was built:
    recovering an overdrawn balance is a BCEA s34 deduction requiring the
    employee's written consent, so it is a decision for a person and never an
    automatic subtraction."""
    result = termination_payout(
        an_input(
            leave_due_days=Decimal("5.000"),
            negative_leave_balance=Decimal("2.500"),
        )
    )

    assert result.total.exact == Decimal("3000.000000"), "the full five days, undiminished"
    assert any("s34 deduction" in w for w in result.trace.warnings)
