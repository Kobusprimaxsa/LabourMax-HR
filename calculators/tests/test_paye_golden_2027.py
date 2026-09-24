"""PAYE against SARS's own worked examples for the 2027 tax year — the year
``reference/ref-2026.03.01.json`` loads.

``test_paye_golden.py`` reproduces G20's examples, which are worked on the
**2026** rates. That proves the method and nothing about the figures anybody is
being asked to verify. These are worked on the **2027** rates, so a band, a
rebate or a credit loaded wrong in REF-2026.03.01 moves an answer here — and
``statutory/tests/test_golden_figures.py`` holds the literals below to the
fixture, so the two cannot quietly disagree.

Two source documents, both downloaded from sars.gov.za on 24 September 2026:

* **PAYE-GEN-01-G01 rev 16**, *Guide for Employers in respect of Tax Deduction
  Tables*, effective 1 March 2026 — §6 "Explanation on how to use the tax
  deduction tables", pages 5 and 6: one worked example each for the weekly,
  fortnightly, monthly and annual tables.
  https://www.sars.gov.za/wp-content/uploads/Ops/Guides/PAYE-GEN-01-G01-Guide-for-Employers-in-respect-of-Tax-Deduction-Tables-External-Guide.pdf
* **PAYE-GEN-01-G21 rev 1**, *Guide for Employers in respect of Employees' Tax
  (2027 tax year)* — the 2027 successor to G20, same examples on the new rates,
  pages 9, 10 and 30 to 34.
  https://www.sars.gov.za/wp-content/uploads/Ops/Guides/PAYE-GEN-01-G21-Guide-for-Employers-iro-Employees-Tax-for-2027-External-Guide.pdf

**Every SARS figure here is a DEDUCTION TABLE figure** and this module uses the
statutory rates (D-212, G01 §5: "Small differences may occur … These methods
are acceptable"). So each test states three things: what is reproduced EXACTLY
(the arithmetic SARS writes out that does not depend on the tables — annual
equivalents, balances of remuneration, the medical credit), our figure PINNED to
the cent, and SARS's published figure held within ``SANITY_BOUND`` of it. The
bound is test-only and not a statutory figure; the pinned figure is what catches
a change in our own arithmetic, which the bound alone is too loose to see.

**Two examples do NOT reproduce, and are kept as strict xfails rather than
dropped** (O-43). SARS's weekly and fortnightly examples convert the monthly
medical scheme fees tax credit by dividing by four weeks and by two fortnights
— "(R376+R376 p/m ÷ 4 weeks)" — which over a 52-week year credits R9 776, not the
R9 024 that twelve months of s6A(2) credit comes to. This module spreads the
annual credit over the periods, as the statutory-rates method does for every
other annual figure. Neither reading is invented; which one a statutory-rates
program must follow is a question for a tax practitioner, and the golden command
prints ``2 xfailed`` every run until somebody answers it. ``strict=True`` means
the day the code changes to agree, the xfail fails and has to be removed.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.paye import PayeInput, employees_tax
from calculators.tests.test_paye_golden import (
    BRACKETS_2027,
    CREDIT_2027,
    PRIMARY_2027,
    SANITY_BOUND,
    within_the_sanctioned_variance,
)

pytestmark = pytest.mark.golden

JUNE_2026 = datetime.date(2026, 6, 30)
OCTOBER_2026 = datetime.date(2026, 10, 31)

TWELVE = Decimal("12")
FORTNIGHTS = Decimal("26")
WEEKS = Decimal("52")
ONE = Decimal("1")


def paye_2027(**overrides) -> PayeInput:
    values = {
        "calculated_for": JUNE_2026,
        "remuneration": Decimal("0.00"),
        "allowable_deductions": Decimal("0.00"),
        "annual_payment": Decimal("0.00"),
        "periods_in_year": TWELVE,
        "periods_worked": ONE,
        "brackets": BRACKETS_2027,
        "rebates": (PRIMARY_2027,),
    }
    values.update(overrides)
    return PayeInput(**values)


def monthly_tax(salary: str) -> Decimal:
    """What one month's table lookup is, on the statutory rates."""
    return employees_tax(paye_2027(remuneration=Decimal(salary))).tax.exact


def test_the_bound_is_the_one_the_2026_file_uses():
    """One sanity bound for both years, so neither file can widen it alone."""
    assert Decimal("0.02") == SANITY_BOUND


# ============================================= G01 rev 16 §6, pages 5 and 6


def test_g01_monthly_tables_example():
    """G01 §6 p5: "A monthly remunerated employee under the age of 65 receives a
    salary of R18 600 and contributes R775 per month to a pension fund and R325
    per month to a retirement annuity fund as well as R900 to a registered
    medical scheme in respect of himself/herself and one dependant.
    … Balance of remuneration R 17 500 … Employees' Tax on balance of
    remuneration according to the monthly tax tables R 1 660 … Less: Medical
    Scheme Fees Tax Credit (R376+R376 p/m) R 752 … Amount of Tax to be deducted
    R 908"."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal("18600.00"),
            allowable_deductions=Decimal("1100.00"),  # R775 pension + R325 RA
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )

    # Exact: the balance of remuneration, and the published monthly credit.
    assert result.annual_equivalent.exact == Decimal("17500") * TWELVE
    assert result.medical_credit_applied.exact / TWELVE == Decimal("752")

    # Ours, pinned: 210 000 × 18% − 17 820 − 9 024 = 10 956 a year, ÷ 12.
    assert result.tax.rounded == Decimal("913.00")
    before_credit = employees_tax(
        paye_2027(remuneration=Decimal("18600.00"), allowable_deductions=Decimal("1100.00"))
    )
    assert before_credit.tax.rounded == Decimal("1665.00")

    assert within_the_sanctioned_variance(before_credit.tax.exact, Decimal("1660"))
    # The credit is the same R752 on both sides, so the gap after it is the R5
    # table gap before it — which, on the smaller R908, is 2.4% and outside the
    # bound. Compare like for like instead of loosening the bound for one test.
    assert before_credit.tax.exact - result.tax.exact == Decimal("752.000000")
    assert result.tax.exact - Decimal("908") == before_credit.tax.exact - Decimal("1660")


def test_g01_annual_tables_example():
    """G01 §6 p6: "An employee under the age of 65 received a salary (retirement
    funding income) of R15 500 per month and contributes R300 per month to a
    pension fund as well as R1 000 per month to a registered medical scheme in
    respect of himself/herself and one dependant. Annual salary (R15 500 x 12)
    R 186 000 … Less: allowable pension fund contributions R 3 600 … Balance of
    remuneration R 182 400 … Employees' tax on balance of remuneration according
    to the annual tax tables R 14 984 … Less: Medical Scheme Fees Tax Credit
    (R376+R376 p/m x 12) R 9 024 … Amount of Tax to be deducted R 5 960"."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal("186000.00"),
            allowable_deductions=Decimal("3600.00"),
            periods_worked=TWELVE,
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )

    assert result.annual_equivalent.exact == Decimal("182400.000000")
    assert result.medical_credit_applied.exact == Decimal("9024.000000")

    # 182 400 × 18% = 32 832, less the R17 820 primary rebate.
    assert result.annual_tax_before_credits.exact - result.rebates_applied.exact == Decimal(
        "15012.000000"
    )
    assert result.tax.rounded == Decimal("5988.00")

    assert within_the_sanctioned_variance(
        result.annual_tax_before_credits.exact - result.rebates_applied.exact, Decimal("14984")
    )
    assert within_the_sanctioned_variance(result.tax.exact, Decimal("5960"))


def test_g01_weekly_tables_example_before_the_credit():
    """G01 §6 p5: "A weekly remunerated employee under the age of 65 receives a
    weekly wage of R3 600 and contributes R160 to a pension fund … Balance of
    remuneration R3 440 … Employees' tax on balance of remuneration according to
    the weekly tax tables R 276"."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal("3600.00"),
            allowable_deductions=Decimal("160.00"),
            periods_in_year=WEEKS,
        )
    )

    assert result.annual_equivalent.exact == Decimal("3440") * WEEKS
    assert result.tax.rounded == Decimal("276.51")
    assert within_the_sanctioned_variance(result.tax.exact, Decimal("276"))


def test_g01_fortnightly_tables_example_before_the_credit():
    """G01 §6 p5: "A fortnightly remunerated employee under the age of 65
    receives a fortnight wage of R8 980 and contributes R320 to a pension fund
    and R160 to a retirement annuity fund … Balance of remuneration R8 500 …
    Employees' tax on balance of remuneration according to the fortnightly tax
    tables R 844"."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal("8980.00"),
            allowable_deductions=Decimal("480.00"),
            periods_in_year=FORTNIGHTS,
        )
    )

    assert result.annual_equivalent.exact == Decimal("8500") * FORTNIGHTS
    assert result.tax.rounded == Decimal("844.62")
    assert within_the_sanctioned_variance(result.tax.exact, Decimal("844"))


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "O-43: SARS's weekly example divides the monthly medical credit by 4 weeks (R188); "
        "the statutory-rates method spreads twelve months' credit over 52 (R173.54)."
    ),
)
def test_g01_weekly_tables_example_after_the_credit():
    """G01 §6 p5: "Less: Medical Scheme Fees Tax Credit (R376+R376 p/m ÷ 4
    weeks) R 188 … Amount of Tax to be deducted R 88"."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal("3600.00"),
            allowable_deductions=Decimal("160.00"),
            periods_in_year=WEEKS,
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )

    assert within_the_sanctioned_variance(result.tax.exact, Decimal("88"))


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "O-43: SARS's fortnightly example divides the monthly medical credit by 2 (R376); "
        "the statutory-rates method spreads twelve months' credit over 26 (R347.08)."
    ),
)
def test_g01_fortnightly_tables_example_after_the_credit():
    """G01 §6 p5: "Less: Medical Scheme Fees Tax Credit (R376+R376 p/m ÷ 2
    weeks) R 376 … Amount of Tax to be deducted R 468"."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal("8980.00"),
            allowable_deductions=Decimal("480.00"),
            periods_in_year=FORTNIGHTS,
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )

    assert within_the_sanctioned_variance(result.tax.exact, Decimal("468"))


def test_the_divergence_the_two_xfails_record_is_the_credit_and_nothing_else():
    """So that O-43 is stated as a figure rather than as a failing test: the
    weekly credit SARS's example uses is 752 ÷ 4, ours is 752 × 12 ÷ 52, and
    the gap between our deduction and SARS's is that difference plus the
    table-versus-rates gap the before-credit test already bounds."""
    with_credit = employees_tax(
        paye_2027(
            remuneration=Decimal("3600.00"),
            allowable_deductions=Decimal("160.00"),
            periods_in_year=WEEKS,
            medical_scheme_members=2,
            medical_credit=CREDIT_2027,
        )
    )
    ours_weekly_credit = with_credit.medical_credit_applied.exact / WEEKS
    sars_weekly_credit = Decimal("752") / Decimal("4")

    assert sars_weekly_credit == Decimal("188")
    assert ours_weekly_credit.quantize(Decimal("0.01")) == Decimal("173.54")
    assert (sars_weekly_credit * WEEKS) - (Decimal("752") * TWELVE) == Decimal("752"), (
        "SARS's weekly conversion credits one extra month a year"
    )


# ========================================= G21 rev 1, the annual equivalent


def test_g21_seven_months_worked():
    """G21 p9: "A monthly paid employee: (under 65) worked for 7 full months at
    one employer and received R110, 000 for the period worked … Calculating
    annual equivalent: R110,000 ÷ 7 x 12 = R188,571 … Tax on annual equivalent
    of R188,571 according to annual tax table R16 156 … Tax on R110,000 for 7
    months worked: R16 156 ÷ 12 x 7 R 9 424,33"."""
    result = employees_tax(
        paye_2027(remuneration=Decimal("110000.00"), periods_worked=Decimal("7"))
    )

    assert result.annual_equivalent.rounded.quantize(Decimal("1")) == Decimal("188571")
    assert result.annual_tax_after_credits.rounded == Decimal("16122.86")
    assert result.tax.rounded == Decimal("9405.00")

    assert within_the_sanctioned_variance(result.annual_tax_after_credits.exact, Decimal("16156"))
    assert within_the_sanctioned_variance(result.tax.exact, Decimal("9424.33"))


@pytest.mark.parametrize(
    ("remuneration", "portion", "periods", "annual_equivalent", "ours", "sars_annual", "sars"),
    [
        # "A weekly remunerated employee (under 65) starts working on the 5th
        # day of a week. He receives R1 090 for the 3 days worked … 3 ÷ 7 =
        # 0.4285 … R1 090 ÷ 0.4285 x 52 = R132 275 … R5 966 … R49"
        ("1090.00", "0.4285", WEEKS, "132275", "49.36", "5966", "49"),
        # "A fortnightly remunerated employee (under 65) starts working on the
        # 7th day of a fortnight period. He receives R3 244 for the 8 days
        # worked … 8 ÷ 14 = 0.5714 … R3 244 ÷ 0.5714 x 26 = R147 609 … R8 761
        # … R193"
        ("3244.00", "0.5714", FORTNIGHTS, "147609", "192.29", "8761", "193"),
        # "A monthly remunerated employee (under 65) starts working on the 16th
        # day of the month which consists of 30 days. He receives R6 500 for
        # the 15 days worked … R6 500 ÷ 0.5 x 12 = R156 000 … R10 294 … R429"
        ("6500.00", "0.5", TWELVE, "156000", "427.50", "10294", "429"),
    ],
    ids=["weekly", "fortnightly", "monthly"],
)
def test_g21_employed_for_a_portion_of_a_pay_period(
    remuneration, portion, periods, annual_equivalent, ours, sars_annual, sars
):
    """G21 p10, "Employee is employed for a portion of a pay period". All
    three run through the ONE formula with the decimal portion in
    ``periods_worked`` (D-213)."""
    result = employees_tax(
        paye_2027(
            remuneration=Decimal(remuneration),
            periods_in_year=periods,
            periods_worked=Decimal(portion),
        )
    )

    assert result.annual_equivalent.rounded.quantize(Decimal("1")) == Decimal(annual_equivalent)
    assert result.tax.rounded == Decimal(ours)
    assert within_the_sanctioned_variance(
        result.annual_tax_after_credits.exact, Decimal(sars_annual)
    )
    assert within_the_sanctioned_variance(result.tax.exact, Decimal(sars))


# ============================================ G21 rev 1, what counts, and when


def test_g21_overtime_is_added_to_the_salary_for_the_period():
    """G21 p33: "Employees' tax on overtime payments is not calculated
    differently from tax on salaries … A monthly paid employee (below 65)
    received R16 000 salary and R900 overtime in June … Tax on R16 900 (salary
    and overtime) according to the monthly tables R 1 551"."""
    assert monthly_tax("16900.00").quantize(Decimal("0.01")) == Decimal("1557.00")
    assert within_the_sanctioned_variance(monthly_tax("16900.00"), Decimal("1551"))


def test_g21_advance_salary_is_taxed_per_month_it_covers():
    """G21 pp32-33: "A monthly paid employee (below 65) received R84 000 in
    October in respect of remuneration that is due to accrue to him in October,
    November and December … Tax on R28 000 (salary per month) according to the
    monthly tables 4 171 … Employees' tax on the advance salary of R84 000
    (R 4 171 x 3) is R12 513"."""
    ours = monthly_tax("28000.00") * 3

    assert ours.quantize(Decimal("0.01")) == Decimal("12483.00")
    assert within_the_sanctioned_variance(monthly_tax("28000.00"), Decimal("4171"))
    assert within_the_sanctioned_variance(ours, Decimal("12513"))


def test_g21_backdated_increase_in_the_current_year():
    """G21 p31: "An increase of R300 per month (backdated from 1 July) is paid
    in December … Tax on R28 300 (December salary) according to the monthly
    tables 4 250 … Less: Tax on R28 000 (salary before increase) 4 171 … Tax on
    increased salary (R300) per month 79 … R79 x 5 … 395 … Employees' tax
    deductible for December 4 645"."""
    per_month = monthly_tax("28300.00") - monthly_tax("28000.00")
    december = monthly_tax("28300.00") + per_month * 5

    assert per_month.quantize(Decimal("0.01")) == Decimal("78.00")
    assert december.quantize(Decimal("0.01")) == Decimal("4629.00")
    assert within_the_sanctioned_variance(monthly_tax("28300.00"), Decimal("4250"))
    assert within_the_sanctioned_variance(december, Decimal("4645"))


def test_g21_quarterly_commission_is_spread_over_the_months_it_was_earned():
    """G21 p30: "Divide the quarterly commission by the months in which it was
    earned (R13 500 ÷ 3) 4 500 … Add: salary for June 17 185 … 21 685 … Tax on
    R21 685 (salary and commission) according to the monthly tables 2 522 …
    Less: Tax on R17 185 (monthly salary) 1 605 … Tax on commission for one
    month 917 … (R917 x 3) 2 751 … Employees' tax deductible for June R4 356"."""
    one_month = monthly_tax("21685.00") - monthly_tax("17185.00")
    june = monthly_tax("17185.00") + one_month * 3

    assert one_month.quantize(Decimal("0.01")) == Decimal("910.80")
    assert june.quantize(Decimal("0.01")) == Decimal("4340.70")
    assert within_the_sanctioned_variance(monthly_tax("21685.00"), Decimal("2522"))
    assert within_the_sanctioned_variance(june, Decimal("4356"))


def test_g21_bonus_in_the_month_it_is_paid():
    """G21 pp33-34: "A monthly paid employee (below 65) received a salary of
    R28 000 and a bonus of R14 800 in October … Annual equivalent of salary
    (R28 000 x 12) 336 000 … Add: bonus (annual payment) 14 800 … Total
    remuneration for October 350 800 … Tax on R350 800 (total remuneration)
    according to the annual tables 53 723 … Less: Tax on R336 000 (annual
    equivalent) according to annual tables 49 945 … Tax on bonus (R14 800)
    3 778 … Employees' Tax deductible for October is R7 949".

    The 2027 twin of the G20 example in ``test_paye_golden.py``: the bonus is
    added to the annual equivalent ONCE, and the difference is its tax."""
    result = employees_tax(
        paye_2027(
            calculated_for=OCTOBER_2026,
            remuneration=Decimal("28000.00"),
            annual_payment=Decimal("14800.00"),
        )
    )

    assert result.annual_equivalent.exact == Decimal("336000.000000"), "salary × 12, bonus out"
    assert result.annual_tax_after_credits.rounded == Decimal("49932.00")
    assert result.tax_on_annual_payment.rounded == Decimal("3848.00")
    assert result.tax.rounded == Decimal("8009.00")

    assert within_the_sanctioned_variance(result.annual_tax_after_credits.exact, Decimal("49945"))
    assert within_the_sanctioned_variance(result.tax_on_annual_payment.exact, Decimal("3778"))
    assert within_the_sanctioned_variance(result.tax.exact, Decimal("7949"))


def test_g21_production_bonus_paid_the_month_after_it_was_earned():
    """G21 p34: "Tax on R40 500 (salary and production bonus for July)
    according to monthly tables 7 954 … Less: tax deducted for July according to
    the monthly tables 4 171 … Tax on production bonus of R12 500 (paid in
    August) 3 783 … Add: tax on salary (R28 000) for August according to the
    monthly table 4 171 … Employees' Tax deductible for August 7 954".

    A bonus that relates to a period is taxed WITH that period, not as an
    annual payment. This is the widest gap in the file, 1.4% on R7 954, and it
    is named rather than hidden: SARS's R7 954 is the tax on a monthly figure
    near R40 870 on the statutory rates, not on R40 500."""
    on_bonus = monthly_tax("40500.00") - monthly_tax("28000.00")
    august = on_bonus + monthly_tax("28000.00")

    assert monthly_tax("40500.00").quantize(Decimal("0.01")) == Decimal("7839.75")
    assert august == monthly_tax("40500.00")
    # SARS's R3 783 on the bonus alone is a difference of two table figures, so
    # a relative bound on it compounds both gaps; the total is what is deducted.
    assert on_bonus.quantize(Decimal("0.01")) == Decimal("3678.75")
    assert within_the_sanctioned_variance(august, Decimal("7954"))
