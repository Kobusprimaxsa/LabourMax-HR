"""PAYE: the branches the golden tests do not reach.

The golden file holds this calculator to published SARS figures. This one holds
it to the Fourth Schedule's own edges — the directives that displace the tables
entirely, the inputs it must refuse rather than guess at, and the boundary
between two bands, tested from both sides (D-158's standing lesson).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from calculators.paye import (
    DIRECTIVE_STATUSES,
    PayeInput,
    PayeInputError,
    TaxBracket,
    TaxStatus,
    employees_tax,
    monthly_medical_credit,
    tax_on,
)
from calculators.tests.test_paye_golden import (
    BRACKETS_2027,
    CREDIT_2027,
    PRIMARY_2027,
    SECONDARY_2027,
)

MARCH = datetime.date(2026, 3, 31)
TWELVE = Decimal("12")


def an_input(**overrides) -> PayeInput:
    values = {
        "calculated_for": MARCH,
        "remuneration": Decimal("20000.00"),
        "allowable_deductions": Decimal("0.00"),
        "annual_payment": Decimal("0.00"),
        "periods_in_year": TWELVE,
        "periods_worked": Decimal("1"),
        "brackets": BRACKETS_2027,
        "rebates": (PRIMARY_2027,),
    }
    values.update(overrides)
    return PayeInput(**values)


# ------------------------------------------------------------- the band walk


def test_the_boundary_between_two_bands_gives_one_answer_from_either_side():
    """R245 100 is band 1's ceiling and band 2's floor, and the published base
    of band 2 is the tax on all of band 1. So the two formulas must meet
    exactly: 245 100 × 18% = 44 118 = band 2's base plus nothing.

    If they did not meet, a rand of income either side of the boundary would
    change the tax by more than the marginal rate — which is exactly what a
    band loaded with the wrong bound looks like, and it is invisible in any
    test that only checks the middle of a band.
    """
    boundary = Decimal("245100")

    from_below = boundary * Decimal("0.18")
    at_boundary = tax_on(boundary, BRACKETS_2027)
    just_above = tax_on(boundary + Decimal("1"), BRACKETS_2027)
    just_below = tax_on(boundary - Decimal("1"), BRACKETS_2027)

    assert at_boundary == from_below == Decimal("44118.00")
    assert just_above - at_boundary == Decimal("0.26")
    assert at_boundary - just_below == Decimal("0.18")


def test_an_income_above_a_closed_top_band_is_refused_rather_than_untaxed():
    """``check_paye_brackets()`` proves the loaded top band is open-ended. If
    something ever hands this function a closed one, the income must not fall
    through the loop and be taxed at nothing."""
    closed = BRACKETS_2027[:-1] + (
        TaxBracket(
            income_from=Decimal("1878600"),
            income_to=Decimal("2000000"),
            base_tax=Decimal("666339"),
            marginal_rate_percent=Decimal("45"),
            table="paye_tax_bracket",
            row_id=7,
        ),
    )

    with pytest.raises(PayeInputError, match="above the highest band"):
        tax_on(Decimal("2500000"), closed)


def test_a_bracket_refuses_a_float():
    with pytest.raises(TypeError, match="not Decimal"):
        TaxBracket(
            income_from=Decimal("0"),
            income_to=245100.0,
            base_tax=Decimal("0"),
            marginal_rate_percent=Decimal("18"),
            table="paye_tax_bracket",
            row_id=1,
        )


# --------------------------------------------------------- the medical credit


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        (0, "0"),
        (1, "376"),
        (2, "752"),
        (3, "1006"),
        (5, "1514"),
    ],
)
def test_the_credit_scale_from_nobody_to_a_household(members, expected):
    assert monthly_medical_credit(members, CREDIT_2027) == Decimal(expected)


def test_no_credit_row_means_no_credit():
    assert monthly_medical_credit(3, None) == Decimal("0")


def test_members_captured_with_no_credit_loaded_warns_rather_than_silently_over_taxing():
    """The employee pays more tax than they should until the rate is loaded.
    That is a figure nobody can see is wrong by looking at the payslip, so it
    has to announce itself in the trace."""
    result = employees_tax(an_input(medical_scheme_members=2, medical_credit=None))

    assert result.medical_credit_applied.exact == Decimal("0.000000")
    assert any("no medical scheme fees tax credit" in w for w in result.trace.warnings)


def test_the_credit_cannot_turn_into_a_refund():
    """s6A is a credit against employees' tax, not a payment. An employee below
    the threshold with a large medical scheme gets nil, never a negative."""
    result = employees_tax(
        an_input(
            remuneration=Decimal("6000.00"),
            medical_scheme_members=4,
            medical_credit=CREDIT_2027,
        )
    )

    assert result.annual_tax_after_credits.exact == Decimal("0.000000")
    assert result.tax.exact == Decimal("0.000000")


# ------------------------------------------------------------------ rebates


def test_a_second_rebate_tier_reduces_the_tax_by_exactly_its_own_amount():
    under = employees_tax(an_input(remuneration=Decimal("30000.00")))
    over_65 = employees_tax(
        an_input(remuneration=Decimal("30000.00"), rebates=(PRIMARY_2027, SECONDARY_2027))
    )

    assert under.rebates_applied.exact == Decimal("17820.000000")
    assert over_65.rebates_applied.exact == Decimal("27585.000000")
    assert (
        under.annual_tax_after_credits.exact - over_65.annual_tax_after_credits.exact
        == SECONDARY_2027.value
    )


# ---------------------------------------------------------------- directives


def test_the_two_directive_statuses_are_named_once():
    assert DIRECTIVE_STATUSES == {
        TaxStatus.DIRECTIVE_FIXED_PCT,
        TaxStatus.DIRECTIVE_FIXED_AMOUNT,
    }


def test_a_fixed_percentage_directive_runs_on_gross_and_credits_nothing():
    """G20 §11.1: "Employers must apply the percentage of employees' tax as
    indicated on the directive prior to taking into account allowable deductions
    for employees' tax purposes (e.g. pension, retirement annuity fund
    contributions, etc.)."

    So the R2 000 pension contribution below does NOT reduce the base, and the
    rebate and the medical credit do not reduce the answer: a directive is an
    instruction from the Commissioner, and "employers may under no circumstances
    deviate from the instructions of the directive".
    """
    result = employees_tax(
        an_input(
            remuneration=Decimal("40000.00"),
            allowable_deductions=Decimal("2000.00"),
            annual_payment=Decimal("10000.00"),
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
            tax_status=TaxStatus.DIRECTIVE_FIXED_PCT,
            directive_number="IRP3B/2026/0001",
            directive_percentage=Decimal("25.000"),
        )
    )

    assert result.tax_on_remuneration.rounded == Decimal("10000.00"), "25% of the full R40 000"
    assert result.tax_on_annual_payment.rounded == Decimal("2500.00")
    assert result.tax.rounded == Decimal("12500.00")
    assert result.rebates_applied.exact == Decimal("0.000000")
    assert result.medical_credit_applied.exact == Decimal("0.000000")
    assert result.annual_tax_before_credits.exact == Decimal("0.000000")


def test_a_fixed_percentage_directive_on_a_negative_gross_says_so():
    result = employees_tax(
        an_input(
            remuneration=Decimal("-500.00"),
            tax_status=TaxStatus.DIRECTIVE_FIXED_PCT,
            directive_percentage=Decimal("25.000"),
        )
    )

    assert any("negative gross" in w for w in result.trace.warnings)


def test_a_fixed_amount_directive_deducts_exactly_what_it_says():
    """Paragraph 11(b): "deduct a specified amount of employees' tax"."""
    result = employees_tax(
        an_input(
            remuneration=Decimal("40000.00"),
            tax_status=TaxStatus.DIRECTIVE_FIXED_AMOUNT,
            directive_number="IRP3C/2026/0002",
            directive_amount=Decimal("1500.00"),
        )
    )

    assert result.tax.rounded == Decimal("1500.00")
    assert result.tax_on_annual_payment.exact == Decimal("0.000000")


def test_an_exempt_employee_has_nothing_deducted():
    """Paragraph 11(a): "refrain from deducting any employees' tax from the
    remuneration of an employee"."""
    result = employees_tax(an_input(remuneration=Decimal("40000.00"), tax_status=TaxStatus.EXEMPT))

    assert result.tax.exact == Decimal("0.000000")
    assert result.annual_tax_after_credits.exact == Decimal("0.000000")


def test_a_foreign_employee_is_taxed_on_the_ordinary_tables():
    """Nationality is an IRP5 distinction, not a different calculation. Relief
    under a double taxation agreement arrives as a DIRECTIVE (O-23)."""
    foreign = employees_tax(an_input(tax_status=TaxStatus.FOREIGN))
    standard = employees_tax(an_input(tax_status=TaxStatus.STANDARD))

    assert foreign.tax.exact == standard.tax.exact
    assert foreign.rebates_applied.exact == Decimal("17820.000000")


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (TaxStatus.DIRECTIVE_FIXED_PCT, "carries no percentage"),
        (TaxStatus.DIRECTIVE_FIXED_AMOUNT, "carries no amount"),
    ],
)
def test_a_directive_with_no_instruction_on_it_is_refused(status, message):
    """There is nothing to fall back to: falling back to the tables IS deviating
    from the directive."""
    with pytest.raises(PayeInputError, match=message):
        employees_tax(an_input(tax_status=status))


def test_an_expired_directive_is_refused_by_name_and_date():
    """ "A tax directive is only valid for the tax year or period stated
    thereon" (G20 §11.1)."""
    with pytest.raises(PayeInputError, match="expired on 28 February 2026"):
        employees_tax(
            an_input(
                tax_status=TaxStatus.DIRECTIVE_FIXED_PCT,
                directive_number="IRP3B/2025/0009",
                directive_percentage=Decimal("25.000"),
                directive_valid_to=datetime.date(2026, 2, 28),
            )
        )


def test_a_directive_valid_on_the_day_itself_is_honoured():
    """Both sides of the boundary, again: the directive is valid THROUGH its
    last day, not up to it."""
    result = employees_tax(
        an_input(
            calculated_for=datetime.date(2026, 2, 28),
            tax_status=TaxStatus.DIRECTIVE_FIXED_AMOUNT,
            directive_amount=Decimal("900.00"),
            directive_valid_to=datetime.date(2026, 2, 28),
        )
    )

    assert result.tax.rounded == Decimal("900.00")


def test_an_unnumbered_expired_directive_still_produces_a_readable_refusal():
    with pytest.raises(PayeInputError, match=r"\(unnumbered\) expired"):
        employees_tax(
            an_input(
                tax_status=TaxStatus.DIRECTIVE_FIXED_AMOUNT,
                directive_amount=Decimal("900.00"),
                directive_valid_to=datetime.date(2026, 2, 28),
            )
        )


# ------------------------------------------------------------- what it refuses


@pytest.mark.parametrize(
    ("worked", "in_year"),
    [(Decimal("0"), TWELVE), (Decimal("-1"), TWELVE), (Decimal("1"), Decimal("0"))],
)
def test_pay_periods_must_be_positive(worked, in_year):
    with pytest.raises(PayeInputError, match="must be positive"):
        employees_tax(an_input(periods_worked=worked, periods_in_year=in_year))


def test_no_brackets_is_refused_rather_than_taxed_at_nothing():
    with pytest.raises(PayeInputError, match="No PAYE brackets"):
        employees_tax(an_input(brackets=()))


def test_no_rebates_is_refused_rather_than_over_taxing_the_employee():
    """An empty list is a resolution failure, not a person who qualifies for
    none — and taxing as though it were takes R17 820 a year too much."""
    with pytest.raises(PayeInputError, match="at least the primary rebate"):
        employees_tax(an_input(rebates=()))


def test_deductions_larger_than_the_remuneration_are_flagged_not_taxed_backwards():
    result = employees_tax(
        an_input(remuneration=Decimal("5000.00"), allowable_deductions=Decimal("6000.00"))
    )

    assert result.annual_equivalent.exact == Decimal("0.000000")
    assert result.tax.exact == Decimal("0.000000")
    assert any("exceed remuneration" in w for w in result.trace.warnings)


# -------------------------------------------------------------------- the trace


def test_every_row_the_calculation_read_is_in_the_trace():
    result = employees_tax(
        an_input(
            rebates=(PRIMARY_2027, SECONDARY_2027),
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )

    assert result.trace.calculator == "paye.employees_tax"
    assert result.trace.calculated_for == MARCH
    assert ("medical_tax_credit_rate", 21) in result.trace.statutory_rows
    assert ("paye_rebate", 11) in result.trace.statutory_rows
    assert ("paye_rebate", 12) in result.trace.statutory_rows
    assert len([row for row in result.trace.statutory_rows if row[0] == "paye_tax_bracket"]) == 7


def test_the_trace_records_the_directive_that_displaced_the_tables():
    result = employees_tax(
        an_input(
            tax_status=TaxStatus.DIRECTIVE_FIXED_PCT,
            directive_number="IRP3B/2026/0001",
            directive_percentage=Decimal("25.000"),
        )
    )

    assert result.trace.inputs["tax_status"] == "directive_fixed_pct"
    assert result.trace.inputs["directive_number"] == "IRP3B/2026/0001"
    assert result.trace.inputs["directive_percentage"] == "25.000"
    assert result.trace.inputs["directive_amount"] == ""


# ---------------------------------------------------------------- properties


REMUNERATION = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("400000"), places=2, allow_nan=False
)


@settings(max_examples=200, deadline=None)
@given(remuneration=REMUNERATION)
def test_tax_is_never_negative_and_never_exceeds_the_income(remuneration):
    result = employees_tax(an_input(remuneration=remuneration))

    assert result.tax.exact >= Decimal("0")
    assert result.tax.exact <= remuneration


@settings(max_examples=200, deadline=None)
@given(lower=REMUNERATION, extra=REMUNERATION)
def test_earning_more_never_takes_home_less(lower, extra):
    """The band walk's real invariant. A band loaded with a wrong base makes the
    tax JUMP at the boundary by more than the extra income, so earning one rand
    more leaves the employee worse off — the thing published cumulative bases
    exist to prevent, and a property test is what finds it across all seven."""
    higher = lower + extra

    on_lower = employees_tax(an_input(remuneration=lower))
    on_higher = employees_tax(an_input(remuneration=higher))

    assert on_higher.tax.exact >= on_lower.tax.exact
    assert (higher - on_higher.tax.exact) >= (lower - on_lower.tax.exact)


@settings(max_examples=100, deadline=None)
@given(remuneration=REMUNERATION, periods=st.sampled_from([1, 2, 6, 7, 11, 12]))
def test_the_annual_equivalent_pro_rates_back_to_what_came_in(remuneration, periods):
    """Paragraph 9(2) in both directions: annualise, then "divide it by the ratio
    which a full year bears to the periods in respect of which the remuneration
    was received"."""
    worked = Decimal(periods)
    result = employees_tax(an_input(remuneration=remuneration, periods_worked=worked))

    assert result.annual_equivalent.exact * worked / TWELVE == pytest.approx(
        remuneration, abs=Decimal("0.01")
    )
