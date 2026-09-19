"""PAYE — employees' tax, Fourth Schedule to the Income Tax Act 58 of 1962.

Pure. Every bracket, rebate and credit arrives carrying the key of the row the
caller read, and every date arrives as an input.

**The method is paragraph 9(1) and 9(2), and SARS writes the formula out** in
PAYE-GEN-01-G20 §7.2: "Total remuneration received/accrued × (Total pay periods
in tax year ÷ Total pay periods worked)". Tax the annual equivalent, take the
rebates and the medical scheme fees tax credit off the annual figure, then
"divide it by the ratio which a full year bears to the periods in respect of
which the remuneration was received or accrued" to get what this period
deducts. An ordinary full month is the same formula with ``periods_worked=1``,
which is why there is one path here and not two.

``periods_worked`` also carries a FRACTION of a period — G20's own worked
example for an employee who starts on the fifth day of a week divides by the
decimal portion 3 ÷ 7 and multiplies by 52. Same field, same formula.

**An annual payment is added to the annual equivalent ONCE** (G20 §12): "the
employees' tax on an annual payment is basically determined by calculating the
annual equivalent of the remuneration earned during the tax period … and adding
the annual payment to the result. The difference between tax on the total result
… and tax on the annual equivalent will result in employees' tax deductible from
annual payment." That difference is deducted in the period the bonus is paid and
is NOT pro-rated — multiplying a bonus by twelve along with the salary is the
classic December over-deduction, and it is what ``test_paye_golden.py``'s last
assertion exists to catch.

**Two sanctioned methods, and this is the statutory-rates one.** SARS publishes
deduction tables and also permits "computer programs that render the same
results as the results that the employers receive when using the statutory rates
of tax" (PAYE-GEN-01-G01 §5), warning in the same breath that "small differences
may occur" between them. This module walks the published bands. Its figures
therefore differ from SARS's own table-worked examples by a few rand a year, by
design and with the Commissioner's own words behind it (D-212).

**Directives (paragraph 9(3) and paragraph 11).** "A directive is an
authorisation issued by SARS to the employer … Employers may under no
circumstances deviate from the instructions of the directive" (G20 §11.1). So a
directive REPLACES the calculation rather than adjusting it: no bands, no
rebates, no medical credit. Paragraph 11 lets the Commissioner direct an
employer to "refrain from deducting any employees' tax", to "deduct a specified
amount", or to "deduct an amount … in accordance with a specified rate or
scale" — the three directive statuses here. A fixed percentage is applied to
GROSS remuneration: "Employers must apply the percentage of employees' tax as
indicated on the directive prior to taking into account allowable deductions for
employees' tax purposes (e.g. pension, retirement annuity fund contributions,
etc.)" (G20 §11.1).

**The caller's half of the contract**, because a pure function cannot check it:

* ``rebates`` are the tiers the employee qualifies for, and age is settled at
  the END of the year of assessment — "the secondary rebate may only be applied
  for individuals who will be 65 years or older on the LAST DAY of the relevant
  year of assessment" (G01 §4). Somebody who turns 65 in January gets it from
  March.
* ``brackets`` are contiguous and the top band is open-ended.
  ``statutory/checks.py::check_paye_brackets()`` proves that at load time; here
  an income that falls off the end raises rather than being taxed at nothing.
* ``directive_valid_to`` comes off the directive itself. "A tax directive is
  only valid for the tax year or period stated thereon."
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from collections.abc import Sequence
from decimal import Decimal

from calculators.base import (
    ZERO,
    CalculationTrace,
    Money,
    StatutoryFigure,
    as_text,
    rows_of,
)

CALCULATOR = "paye.employees_tax"

#: Percentages are stored as percentages, so this is arithmetic, not a rate.
PER_CENT = Decimal("100")

#: The medical scheme fees tax credit is a MONTHLY figure (s6A(2)), and the
#: annual credit is twelve of them regardless of how often the employee is paid.
#: Twelve months in a year is a fact about the calendar, like sixty minutes in an
#: hour — never a figure a gazette carries.
MONTHS_IN_YEAR = Decimal("12")


class PayeInputError(ValueError):
    """The input cannot be priced at all, and guessing would be worse.

    Distinct from a warning: a warning goes on the payslip's trace and the
    payroll run continues. This stops it, because there is no defensible figure
    to deduct — an expired directive, a directive with no instruction on it, or
    a band structure an income falls outside of.
    """


class TaxStatus(enum.StrEnum):
    """Mirrors ``employees.EmployeeTaxProfile.TaxStatus``, value for value.

    Duplicated rather than imported because a calculator may not import a model.
    ``payroll/tests/test_paye_boundary.py`` asserts the two never drift apart,
    which is the only honest way to hold a copy.
    """

    STANDARD = "standard"
    DIRECTIVE_FIXED_PCT = "directive_fixed_pct"
    DIRECTIVE_FIXED_AMOUNT = "directive_fixed_amount"
    EXEMPT = "exempt"
    FOREIGN = "foreign"


#: The two statuses that carry an instruction from the Commissioner and displace
#: the bands entirely.
DIRECTIVE_STATUSES = frozenset({TaxStatus.DIRECTIVE_FIXED_PCT, TaxStatus.DIRECTIVE_FIXED_AMOUNT})


@dataclasses.dataclass(frozen=True)
class TaxBracket:
    """One published band: "R 44 118 + 26% of taxable income above R 245 100".

    Four numbers off one ``paye_tax_bracket`` row, so the row key is carried
    once rather than repeated on four ``StatutoryFigure``s
    (``calculators.base.Sourced``). ``income_to`` is None on the top band —
    "R 1 878 601 and above".
    """

    income_from: Decimal
    income_to: Decimal | None
    base_tax: Decimal
    marginal_rate_percent: Decimal
    table: str
    row_id: int

    def __post_init__(self):
        values = (self.income_from, self.base_tax, self.marginal_rate_percent)
        if self.income_to is not None:
            values += (self.income_to,)
        for value in values:
            if not isinstance(value, Decimal):
                raise TypeError(
                    f"{self.table}.{self.row_id} carries {type(value).__name__}, not Decimal. "
                    "Money is Decimal everywhere in this system (invariant 6)."
                )


@dataclasses.dataclass(frozen=True)
class MedicalCredit:
    """The s6A credit's three published monthly figures, off one row."""

    main_member_monthly: Decimal
    first_dependant_monthly: Decimal
    additional_dependant_monthly: Decimal
    table: str
    row_id: int


@dataclasses.dataclass(frozen=True)
class PayeInput:
    """One employee, one tax period. Frozen, Decimal, and no clock."""

    calculated_for: datetime.date
    #: Remuneration for the periods worked, EXCLUDING any annual payment.
    remuneration: Decimal
    #: Paragraph 2(4) deductions for the same periods — pension, provident and
    #: retirement annuity fund contributions the employer must take off before
    #: applying the tables. Never subtracted on a directive path.
    allowable_deductions: Decimal
    #: A bonus, leave encashment or other payment made without reference to a
    #: period. Added to the annual equivalent ONCE.
    annual_payment: Decimal
    #: "Total pay periods in tax year" — 12, 26 or 52.
    periods_in_year: Decimal
    #: "Total pay periods worked", or a decimal portion of one.
    periods_worked: Decimal
    brackets: Sequence[TaxBracket]
    rebates: Sequence[StatutoryFigure]
    #: Main member plus dependants, as captured on the tax profile.
    medical_scheme_members: int = 0
    medical_credit: MedicalCredit | None = None

    tax_status: TaxStatus = TaxStatus.STANDARD
    directive_number: str = ""
    directive_percentage: Decimal | None = None
    directive_amount: Decimal | None = None
    directive_valid_to: datetime.date | None = None


@dataclasses.dataclass(frozen=True)
class PayeResult:
    """Every figure the payslip, the trace and a dispute need.

    On a directive path the four annual figures are all zero: a directive
    displaces the bands, the rebates and the credit rather than adjusting them,
    and reporting what the tables WOULD have produced would put a number on the
    payslip that nothing deducted.
    """

    #: What this period deducts, in total.
    tax: Money
    #: The pro-rated share of the annual tax on the ordinary remuneration.
    tax_on_remuneration: Money
    #: The whole difference the annual payment made, deducted in this period.
    tax_on_annual_payment: Money
    annual_equivalent: Money
    annual_tax_before_credits: Money
    annual_tax_after_credits: Money
    rebates_applied: Money
    medical_credit_applied: Money
    trace: CalculationTrace


def tax_on(taxable: Decimal, brackets: Sequence[TaxBracket]) -> Decimal:
    """The published formula for the band this income falls in.

    Bands are half-open, ``[income_from, income_to)``, the same convention every
    effective-dated row in this schema uses. At an exact boundary that choice is
    arithmetically immaterial — the cumulative base of the upper band IS the tax
    at the lower band's top — and the golden test proves it from both sides,
    which is what would catch a base loaded one band out.
    """
    for band in brackets:
        if band.income_to is None or taxable < band.income_to:
            above = taxable - band.income_from
            return band.base_tax + above * band.marginal_rate_percent / PER_CENT
    raise PayeInputError(
        f"Taxable income of {taxable} falls above the highest band supplied, which ends at "
        f"{brackets[-1].income_to}. The top band must be open-ended — see "
        f"statutory/checks.py::check_paye_brackets()."
    )


def monthly_medical_credit(members: int, credit: MedicalCredit | None) -> Decimal:
    """s6A(2): the taxpayer, the first dependant, then each additional one."""
    if credit is None or members <= 0:
        return ZERO
    amount = credit.main_member_monthly
    if members >= 2:
        amount += credit.first_dependant_monthly
    if members > 2:
        amount += credit.additional_dependant_monthly * (members - 2)
    return amount


def _refuse_an_uncomputable_input(data: PayeInput) -> None:
    if data.periods_in_year <= ZERO or data.periods_worked <= ZERO:
        raise PayeInputError(
            f"Pay periods must be positive: {data.periods_worked} worked of "
            f"{data.periods_in_year} in the year. The annual equivalent divides by the "
            f"periods worked (paragraph 9(2))."
        )

    if data.tax_status not in DIRECTIVE_STATUSES:
        return

    if data.directive_valid_to is not None and data.calculated_for > data.directive_valid_to:
        raise PayeInputError(
            f"Directive {data.directive_number or '(unnumbered)'} expired on "
            f"{data.directive_valid_to:%d %B %Y} and this calculation is for "
            f"{data.calculated_for:%d %B %Y}. A tax directive is only valid for the tax year "
            f"or period stated on it; the employer must obtain a new one."
        )

    if data.tax_status is TaxStatus.DIRECTIVE_FIXED_PCT and data.directive_percentage is None:
        raise PayeInputError(
            "A fixed-percentage directive carries no percentage. The instruction on the "
            "directive is the whole calculation, and an employer may under no circumstances "
            "deviate from it — including by falling back to the tables."
        )

    if data.tax_status is TaxStatus.DIRECTIVE_FIXED_AMOUNT and data.directive_amount is None:
        raise PayeInputError(
            "A fixed-amount directive carries no amount. The instruction on the directive is "
            "the whole calculation, and there is nothing here to fall back to."
        )


def _refuse_a_table_calculation_with_nothing_to_read(data: PayeInput) -> None:
    if not data.brackets:
        raise PayeInputError(
            "No PAYE brackets were supplied. Every statutory figure is loaded reference "
            "data; nothing here knows a rate."
        )
    if not data.rebates:
        raise PayeInputError(
            "No PAYE rebates were supplied. Every natural person gets at least the primary "
            "rebate (s6(2)(a)), so an empty list is a resolution failure, not a person who "
            "qualifies for none — and deducting as though it were over-taxes them."
        )


def employees_tax(data: PayeInput) -> PayeResult:
    """Employees' tax for one employee for one tax period."""
    warnings: list[str] = []
    _refuse_an_uncomputable_input(data)

    balance = data.remuneration - data.allowable_deductions
    if balance < ZERO:
        # The caller handed in paragraph 2(4) deductions larger than the
        # remuneration they come out of. That is a capture error upstream, and
        # a negative annual equivalent would tax backwards.
        warnings.append(
            f"Allowable deductions ({data.allowable_deductions}) exceed remuneration "
            f"({data.remuneration}); the balance of remuneration is treated as nil. "
            f"Check what was captured."
        )
        balance = ZERO

    annual_equivalent = balance * data.periods_in_year / data.periods_worked
    share = data.periods_worked / data.periods_in_year

    rebate_total = ZERO
    medical_credit = ZERO
    annual_before = ZERO
    annual_after = ZERO

    if data.tax_status is TaxStatus.EXEMPT:
        # Paragraph 11(a): the Commissioner may direct an employer to "refrain
        # from deducting any employees' tax from the remuneration of an
        # employee".
        tax_on_remuneration = ZERO
        tax_on_annual_payment = ZERO

    elif data.tax_status is TaxStatus.DIRECTIVE_FIXED_AMOUNT:
        # Paragraph 11(b): "deduct a specified amount of employees' tax". Taken
        # as the amount for one pay period, which is how an IRP3(c) directive is
        # written for the monthly employees both our sectors run.
        tax_on_remuneration = data.directive_amount
        tax_on_annual_payment = ZERO

    elif data.tax_status is TaxStatus.DIRECTIVE_FIXED_PCT:
        # Paragraph 11(c) and G20 §11.1: the percentage is applied "prior to
        # taking into account allowable deductions", so it runs on gross
        # remuneration including the annual payment, and nothing is credited
        # against it.
        gross = data.remuneration + data.annual_payment
        tax_on_remuneration = data.remuneration * data.directive_percentage / PER_CENT
        tax_on_annual_payment = data.annual_payment * data.directive_percentage / PER_CENT
        if gross < ZERO:
            warnings.append(f"Directive percentage applied to a negative gross of {gross}.")

    else:
        # STANDARD, and FOREIGN with it: a foreign employee's South African
        # remuneration is taxed on the ordinary tables. Relief under a double
        # taxation agreement reaches the payslip as a DIRECTIVE, which is a
        # different status above (O-23).
        _refuse_a_table_calculation_with_nothing_to_read(data)

        if data.medical_scheme_members > 0 and data.medical_credit is None:
            warnings.append(
                f"{data.medical_scheme_members} medical scheme member(s) captured but no "
                f"medical scheme fees tax credit was supplied; no credit applied. The "
                f"employee is over-taxed until the rate is loaded."
            )

        rebate_total = sum((figure.value for figure in data.rebates), ZERO)
        medical_credit = (
            monthly_medical_credit(data.medical_scheme_members, data.medical_credit)
            * MONTHS_IN_YEAR
        )

        annual_before = tax_on(annual_equivalent, data.brackets)
        annual_after = max(annual_before - rebate_total - medical_credit, ZERO)
        tax_on_remuneration = annual_after * share

        if data.annual_payment:
            with_payment = tax_on(annual_equivalent + data.annual_payment, data.brackets)
            tax_on_annual_payment = (
                max(with_payment - rebate_total - medical_credit, ZERO) - annual_after
            )
        else:
            tax_on_annual_payment = ZERO

    total = tax_on_remuneration + tax_on_annual_payment

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            remuneration=data.remuneration,
            allowable_deductions=data.allowable_deductions,
            annual_payment=data.annual_payment,
            periods_in_year=data.periods_in_year,
            periods_worked=data.periods_worked,
            medical_scheme_members=data.medical_scheme_members,
            tax_status=data.tax_status.value,
            directive_number=data.directive_number,
            directive_percentage=data.directive_percentage,
            directive_amount=data.directive_amount,
        ),
        statutory_rows=rows_of(
            *data.brackets,
            *data.rebates,
            *([data.medical_credit] if data.medical_credit is not None else []),
        ),
        outputs=as_text(
            annual_equivalent=Money.of(annual_equivalent).exact,
            annual_tax_before_credits=Money.of(annual_before).exact,
            rebates_applied=Money.of(rebate_total).exact,
            medical_credit_applied=Money.of(medical_credit).exact,
            annual_tax_after_credits=Money.of(annual_after).exact,
            tax_on_remuneration=Money.of(tax_on_remuneration).exact,
            tax_on_annual_payment=Money.of(tax_on_annual_payment).exact,
            tax=Money.of(total).exact,
        ),
        warnings=tuple(warnings),
    )

    return PayeResult(
        tax=Money.of(total),
        tax_on_remuneration=Money.of(tax_on_remuneration),
        tax_on_annual_payment=Money.of(tax_on_annual_payment),
        annual_equivalent=Money.of(annual_equivalent),
        annual_tax_before_credits=Money.of(annual_before),
        annual_tax_after_credits=Money.of(annual_after),
        rebates_applied=Money.of(rebate_total),
        medical_credit_applied=Money.of(medical_credit),
        trace=trace,
    )
