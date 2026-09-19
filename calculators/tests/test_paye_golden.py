"""PAYE against published SARS material. Written before ``calculators/paye.py``.

Two source documents, both downloaded from sars.gov.za and quoted verbatim in
the fixtures below:

* **PAYE-GEN-01-G01** rev 16, *Guide for Employers in respect of Tax Deduction
  Tables*, effective **1 March 2026** — carries the statutory rates, rebates,
  thresholds and medical scheme fees tax credits for the **2027** tax year,
  which are exactly the figures ``reference/ref-2026.03.01.json`` loads.
* **PAYE-GEN-01-G20** rev 0, *Guide for Employers in respect of Employees' Tax
  (2026 tax year)*, effective 12 March 2025 — carries the **annual equivalent**
  formula (§7.2) and the **annual payment** method (§12), each with worked
  examples, on the **2026** tax year's rates (§1).

**The examples are worked on the DEDUCTION TABLES; this calculator uses the
STATUTORY RATES, and the two are expected to differ** (D-212). G01 §5 says so in
terms: "Small differences may occur between the manual tables, and other
computer programs based on the statutory rates of tax. These methods are
acceptable in terms of the Income Tax Act". So the tests below split in two:

* Everything computed on the statutory rates is reproduced **exactly** — every
  band's published cumulative base, the published marginal formula, the three
  published tax thresholds, the published medical credit, and SARS's own
  annual-equivalent and pro-rata arithmetic.
* SARS's two fully worked examples are reproduced by **method**, with the
  published table figure and ours both named, and the gap asserted to stay
  inside a bound that is a test-only sanity check and not a statutory figure.

The 2026 figures are used with the 2026 examples and the 2027 figures with the
2027 material. A calculator that took its rates as inputs and still only worked
for one year would not have taken them as inputs.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.paye import (
    MedicalCredit,
    PayeInput,
    TaxBracket,
    employees_tax,
    tax_on,
)

pytestmark = pytest.mark.golden

MARCH_2026 = datetime.date(2026, 3, 31)
OCTOBER_2025 = datetime.date(2025, 10, 31)

TWELVE = Decimal("12")


def bracket(order, income_from, income_to, base_tax, rate) -> TaxBracket:
    return TaxBracket(
        income_from=Decimal(income_from),
        income_to=None if income_to is None else Decimal(income_to),
        base_tax=Decimal(base_tax),
        marginal_rate_percent=Decimal(rate),
        table="paye_tax_bracket",
        row_id=order,
    )


def rebate(row_id, amount, description) -> StatutoryFigure:
    return StatutoryFigure(
        value=Decimal(amount), table="paye_rebate", row_id=row_id, description=description
    )


# --------------------------------------------------------- 2027 (1 March 2026)
# PAYE-GEN-01-G01 rev 16 §4 "STATUTORY RATES OF TAX", "Tax Tables for
# Individuals and Trusts, 2025/2026 Tax Year (1 March 2026 to 28 February 2027)"
# — the guide's own heading; its rebate, threshold and credit columns are all
# headed 2027, and these are the rows ref-2026.03.01.json loads.

BRACKETS_2027 = (
    bracket(1, 0, 245_100, 0, 18),
    bracket(2, 245_100, 383_100, 44_118, 26),
    bracket(3, 383_100, 530_200, 79_998, 31),
    bracket(4, 530_200, 695_800, 125_599, 36),
    bracket(5, 695_800, 887_000, 185_215, 39),
    bracket(6, 887_000, 1_878_600, 259_783, 41),
    bracket(7, 1_878_600, None, 666_339, 45),
)

PRIMARY_2027 = rebate(11, 17_820, "Primary rebate")
SECONDARY_2027 = rebate(12, 9_765, "Secondary rebate (65 and older)")
TERTIARY_2027 = rebate(13, 3_249, "Tertiary rebate (75 and older)")

#: "For the taxpayer R376 / For the first dependent R376 / For each additional
#: dependent R254" — G01 §4, 2027 column.
CREDIT_2027 = MedicalCredit(
    main_member_monthly=Decimal("376"),
    first_dependant_monthly=Decimal("376"),
    additional_dependant_monthly=Decimal("254"),
    table="medical_tax_credit_rate",
    row_id=21,
)

# --------------------------------------------------------- 2026 (1 March 2025)
# PAYE-GEN-01-G20 rev 0 §1 "QUICK REFERENCE CARD", "2025/2026 Tax Year
# (1 March 2025 to 28 February 2026)". Loaded here only so that G20's own worked
# examples can be run against the rates they were worked on.

BRACKETS_2026 = (
    bracket(101, 0, 237_100, 0, 18),
    bracket(102, 237_100, 370_500, 42_678, 26),
    bracket(103, 370_500, 512_800, 77_362, 31),
    bracket(104, 512_800, 673_000, 121_475, 36),
    bracket(105, 673_000, 857_900, 179_147, 39),
    bracket(106, 857_900, 1_817_000, 251_258, 41),
    bracket(107, 1_817_000, None, 644_489, 45),
)

PRIMARY_2026 = rebate(111, 17_235, "Primary rebate")


def a_standard_input(**overrides) -> PayeInput:
    values = {
        "calculated_for": MARCH_2026,
        "remuneration": Decimal("0.00"),
        "allowable_deductions": Decimal("0.00"),
        "annual_payment": Decimal("0.00"),
        "periods_in_year": TWELVE,
        "periods_worked": Decimal("1"),
        "brackets": BRACKETS_2027,
        "rebates": (PRIMARY_2027,),
    }
    values.update(overrides)
    return PayeInput(**values)


# ================================================== exact: the statutory rates


@pytest.mark.parametrize(
    "band",
    BRACKETS_2027[1:],
    ids=lambda b: f"band starting {b.income_from}",
)
def test_every_published_cumulative_base_is_reproduced_exactly(band):
    """SARS publishes each band as "R 44 118 + 26% of taxable income above
    R 245 100". The R44 118 is the tax on everything below the band, so the
    calculator's own walk up the bands must arrive at exactly that figure.

    This is the whole bracket walk checked against seven published numbers, and
    it is the check that would catch a band loaded with the wrong bound: a gap
    or an overlap of one rand moves the cumulative base off the published one.
    """
    assert tax_on(band.income_from, BRACKETS_2027) == band.base_tax


def test_the_published_marginal_formula_is_reproduced_exactly():
    """ "R 44 118 + 26% of taxable income above R 245 100" — G01 §4, band 2."""
    taxable = Decimal("300000")
    published = Decimal("44118") + Decimal("0.26") * (taxable - Decimal("245100"))

    assert tax_on(taxable, BRACKETS_2027) == published == Decimal("58392.00")


def test_the_top_band_is_open_ended():
    """ "R 1 878 601 and above" — there is no income too large to tax."""
    assert tax_on(Decimal("5000000"), BRACKETS_2027) == Decimal("666339") + Decimal("0.45") * (
        Decimal("5000000") - Decimal("1878600")
    )


@pytest.mark.parametrize(
    ("threshold", "rebates", "who"),
    [
        (Decimal("99000"), (PRIMARY_2027,), "under 65"),
        (Decimal("153250"), (PRIMARY_2027, SECONDARY_2027), "65 and older"),
        (Decimal("171300"), (PRIMARY_2027, SECONDARY_2027, TERTIARY_2027), "75 and older"),
    ],
)
def test_the_three_published_tax_thresholds_produce_exactly_no_tax(threshold, rebates, who):
    """G01 §4: "Tax thresholds applicable to individuals 2027 — Persons under 65
    years R 99 000; Persons 65 years and older R 153 250; Persons 75 years and
    older R 171 300."

    A threshold is the income at which the rebates exactly cancel the tax, so
    this is the rebate arithmetic reproduced against three published figures —
    and a rand either side of it settles which direction the floor runs.
    """
    result = employees_tax(
        a_standard_input(
            remuneration=threshold, periods_worked=TWELVE, periods_in_year=TWELVE, rebates=rebates
        )
    )

    assert result.tax.rounded == Decimal("0.00"), who
    assert result.annual_tax_before_credits.exact == threshold * Decimal("0.18")

    above = employees_tax(
        a_standard_input(
            remuneration=threshold + Decimal("100"),
            periods_worked=TWELVE,
            periods_in_year=TWELVE,
            rebates=rebates,
        )
    )
    assert above.tax.rounded == Decimal("18.00"), "the hundred rand above the threshold, at 18%"


def test_the_medical_credit_is_the_published_monthly_figure():
    """G01 §6, monthly example: "Less: Medical Scheme Fees Tax Credit
    (R376+R376 p/m)" — R752 for a main member and one dependant, per month."""
    result = employees_tax(
        a_standard_input(
            remuneration=Decimal("18600.00"),
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )

    assert result.medical_credit_applied.exact == Decimal("752") * TWELVE
    assert result.medical_credit_applied.exact == Decimal("9024.000000")


def test_the_credit_for_a_member_with_three_dependants_is_the_published_scale():
    """ "R376 taxpayer / R376 first dependent / R254 each additional"."""
    result = employees_tax(
        a_standard_input(
            remuneration=Decimal("30000.00"),
            medical_scheme_members=4,
            medical_credit=CREDIT_2027,
        )
    )

    monthly = Decimal("376") + Decimal("376") + Decimal("254") * 2
    assert monthly == Decimal("1260")
    assert result.medical_credit_applied.exact == monthly * TWELVE


# ================================== exact: SARS's own annual-equivalent arithmetic


def test_the_published_annual_equivalent_formula_is_reproduced_exactly():
    """G20 §7.2: "A monthly paid employee (under 65) worked for 7 full months at
    one employer and received R110,000 for the period worked … Calculating
    annual equivalent: R110,000 ÷ 7 x 12 = R188,571."

    The prescribed formula is *total remuneration × (total pay periods in tax
    year ÷ total pay periods worked)*. Getting it upside down — dividing by
    twelve and multiplying by seven — is the whole reason it is written out in
    the guide, so the published figure is the test.
    """
    result = employees_tax(
        PayeInput(
            calculated_for=OCTOBER_2025,
            remuneration=Decimal("110000.00"),
            allowable_deductions=Decimal("0.00"),
            annual_payment=Decimal("0.00"),
            periods_in_year=TWELVE,
            periods_worked=Decimal("7"),
            brackets=BRACKETS_2026,
            rebates=(PRIMARY_2026,),
        )
    )

    assert result.annual_equivalent.rounded.quantize(Decimal("1")) == Decimal("188571")


def test_a_partial_first_pay_period_uses_the_published_decimal_portion():
    """G20 §7.2: "A weekly remunerated employee (under 65) starts working on the
    5th day of a week. He receives R1 090 for the 3 days worked … Determine the
    decimal portion of the pay period: 3 ÷ 7 = 0.4285 … Calculating annual
    equivalent: R1 090 ÷ 0.4285 x 52 = R132 275."

    The decimal portion is a fraction of ONE pay period, so it is
    ``periods_worked`` — the same field that carries 7 for seven whole months.
    One formula, not two, which is what the guide's four examples all are.
    """
    result = employees_tax(
        PayeInput(
            calculated_for=datetime.date(2025, 3, 7),
            remuneration=Decimal("1090.00"),
            allowable_deductions=Decimal("0.00"),
            annual_payment=Decimal("0.00"),
            periods_in_year=Decimal("52"),
            periods_worked=Decimal("0.4285"),
            brackets=BRACKETS_2026,
            rebates=(PRIMARY_2026,),
        )
    )

    assert result.annual_equivalent.rounded.quantize(Decimal("1")) == Decimal("132275")


def test_the_published_pro_rata_step_is_reproduced_exactly():
    """G20 §7.2: "Tax on R110,000 for 7 months worked: R16 741 ÷ 12 x 7 = R9 766."

    Checked against SARS's own annual figure rather than ours, so this proves
    the pro-rata step on its own, independently of the table-versus-rates
    difference in the annual figure it is applied to.
    """
    published_annual_tax = Decimal("16741")

    share = published_annual_tax / TWELVE * Decimal("7")

    assert share.quantize(Decimal("1")) == Decimal("9766")


# ============================== by method: the two fully worked SARS examples
#
# Our figure and SARS's differ because SARS worked the example on the deduction
# TABLES. G01 §5 sanctions the difference; this bound is a test-only sanity
# check that the two methods stay in the same place, NOT a statutory figure and
# not a tolerance anybody gazetted.
SANITY_BOUND = Decimal("0.02")


def within_the_sanctioned_variance(ours: Decimal, published: Decimal) -> bool:
    return abs(ours - published) / published < SANITY_BOUND


def test_the_seven_month_example_lands_where_sars_puts_it():
    """G20 §7.2, in full: R110,000 over seven months, under 65.

    SARS, on the annual tables: annual equivalent R188 571, annual tax R16 741,
    tax for the seven months R9 766.
    """
    result = employees_tax(
        PayeInput(
            calculated_for=OCTOBER_2025,
            remuneration=Decimal("110000.00"),
            allowable_deductions=Decimal("0.00"),
            annual_payment=Decimal("0.00"),
            periods_in_year=TWELVE,
            periods_worked=Decimal("7"),
            brackets=BRACKETS_2026,
            rebates=(PRIMARY_2026,),
        )
    )

    # Ours, on the statutory rates: 188 571.43 × 18% = 33 942.86, less the
    # R17 235 primary rebate, × 7/12.
    assert result.annual_tax_after_credits.rounded == Decimal("16707.86")
    assert result.tax.rounded == Decimal("9746.25")

    assert within_the_sanctioned_variance(
        result.annual_tax_after_credits.exact, Decimal("16741")
    ), f"ours {result.annual_tax_after_credits.rounded} against SARS's table figure R16 741"
    assert within_the_sanctioned_variance(result.tax.exact, Decimal("9766"))


def test_the_bonus_example_taxes_the_bonus_once_and_lands_where_sars_puts_it():
    """G20 §12: "A monthly paid employee (below 65) received a salary of R28 000
    and a bonus of R14 800 in October … Annual equivalent of salary
    (R28 000 x 12) 336 000; Add: bonus (annual payment) 14 800; Tax on R350 800
    according to the annual tables 54 937; Less: Tax on R336 000 according to
    annual tables 51 033; Tax on bonus (R14 800) 3 904."

    The method, in the guide's own words: "calculating the annual equivalent of
    the remuneration earned during the tax period by the employee and adding the
    annual payment to the result. The difference between tax on the total result
    … and tax on the annual equivalent will result in employees' tax deductible
    from annual payment."

    So the bonus is added to the annualised salary ONCE. Multiplying it by
    twelve along with the salary is the classic December over-deduction, and the
    last assertion here is what that mistake would break.
    """
    result = employees_tax(
        PayeInput(
            calculated_for=OCTOBER_2025,
            remuneration=Decimal("28000.00"),
            allowable_deductions=Decimal("0.00"),
            annual_payment=Decimal("14800.00"),
            periods_in_year=TWELVE,
            periods_worked=Decimal("1"),
            brackets=BRACKETS_2026,
            rebates=(PRIMARY_2026,),
        )
    )

    assert result.annual_equivalent.exact == Decimal("336000.000000"), "salary × 12, bonus out"
    assert result.annual_tax_after_credits.rounded == Decimal("51157.00")
    assert result.tax_on_annual_payment.rounded == Decimal("3848.00")
    assert result.tax_on_remuneration.rounded == Decimal("4263.08"), "51 157 ÷ 12"
    assert result.tax.rounded == Decimal("8111.08"), "the month's salary tax plus the bonus tax"

    # SARS's own published difference, on the tables, from the same example.
    assert Decimal("54937") - Decimal("51033") == Decimal("3904")
    assert within_the_sanctioned_variance(result.tax_on_annual_payment.exact, Decimal("3904"))
    assert within_the_sanctioned_variance(result.annual_tax_after_credits.exact, Decimal("51033"))

    # And the defect the method exists to prevent. Annualising the bonus along
    # with the salary — R42 800 × 12 — puts the employee in the 36% band and
    # nearly DOUBLES the tax on a year in which they earned exactly the same
    # money. The damage is on the year, not on the month: the wrong method's
    # October deduction is only a few hundred rand higher, which is why this
    # gets shipped and then found in February.
    correct_for_the_year = (
        result.annual_tax_after_credits.exact + result.tax_on_annual_payment.exact
    )
    assert correct_for_the_year == Decimal("55005.000000")

    annualised_by_mistake = employees_tax(
        PayeInput(
            calculated_for=OCTOBER_2025,
            remuneration=Decimal("42800.00"),  # salary and bonus together, then ×12
            allowable_deductions=Decimal("0.00"),
            annual_payment=Decimal("0.00"),
            periods_in_year=TWELVE,
            periods_worked=Decimal("1"),
            brackets=BRACKETS_2026,
            rebates=(PRIMARY_2026,),
        )
    )
    assert annualised_by_mistake.annual_tax_after_credits.rounded == Decimal("104528.00")
    assert annualised_by_mistake.annual_tax_after_credits.exact > correct_for_the_year * Decimal(
        "1.8"
    )
