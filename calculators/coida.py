"""COIDA — one employee's earnings for one assessment period, capped.

Compensation for Occupational Injuries and Diseases Act 130 of 1993. Pure, like
every calculator here: the ceiling arrives as a ``StatutoryFigure`` carrying the
key of the row the caller read, and every date arrives as an input.

**COIDA is not a payslip deduction.** Nothing is taken from the employee. The
employer declares its employees' earnings to the Compensation Fund on the annual
Return of Earnings (form CF-2A) and is assessed on them. What payroll owes that
return is the ACCUMULATION: each employee's earnings for the assessment period,
capped at the Minister's maximum. This module is that and nothing more — the
assessment itself (earnings × the employer's tariff, floored at the minimum
assessment) is P8's return, and needs a tariff this build does not hold.

**The cap is annual, per employee, applied once at the end of the period, and
never pro-rated.** The Department's own words, in the notice that sets the
figure — GN 2390 of 2024, GG 50386, 27 March 2024, page 5, and GN 1723 of 2023,
GG 48337, 30 March 2023, in the same terms:

    "A Maximum Earnings is applied annually at the end of the assessment period
    to the individual employee's annual total earnings, not per month. Full
    annual maximum earnings will apply irrespective of the number of months the
    employee was employed in the 2023 ROE Season. … Examples: 1. If an employee
    has earned a total earnings of R600 000.00 from the employer during the
    period as stated above, the amount should be capped at R568 959.00 and be
    declared as such. 2. if an employee has earned total earnings of any amount
    below R568 959.00, the total earnings must be declared as is, regardless of
    whether the said employee worked for a full year or part year."

So a mid-year starter gets the whole ceiling, not seven twelfths of it, and the
function takes the period's total rather than twelve monthly figures. Earnings
exactly AT the ceiling are declared in full: nothing exceeds it.

**The assessment period is an INPUT.** The notices state it — "(01 March 2023
to 29 February 2024) – Assessment Period" (GN 2390 of 2024), and the CF-2A in
Notice 3910 of 2026, GG 54577, heads its columns "Actual Earnings: 01/03/2025 -
28/02/2026 Provisional Earnings: 01/03/2026 - 28/02/2027". Those dates coincide
with the SARS tax year, but the instrument is a different one and nothing here
reads a ``tax_year`` row for them: a Minister who moved the assessment period
would not move the tax year.

**What counts as earnings is the component's flag, read — never restated**
(D-89). Each line arrives with ``is_coida_base`` as the caller read it off the
payroll component, which copies it from the SARS source code. This module does
not know that overtime is 3607, or that O-06 still asks whether 3607 belongs in
the base at all; the flag carries the answer that is loaded, and changing it is
a data change that reaches this figure without touching this file. O-15's
severance question is the same: 3901 is flagged out, and that is the flag's
answer, not this module's.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence
from decimal import Decimal

from calculators.base import ZERO, CalculationTrace, Money, StatutoryFigure, as_text, rows_of

CALCULATOR = "coida.assessment_earnings"


class CoidaInputError(ValueError):
    """The period cannot be accumulated, and guessing would be worse."""


@dataclasses.dataclass(frozen=True)
class CoidaEarning:
    """One finalised payslip line, as the Return of Earnings sees it."""

    source_code: str
    amount: Decimal
    #: ``payroll_component.is_coida_base`` as read. The whole of the "is this
    #: earnings" decision, made elsewhere and carried here.
    is_coida_base: bool


@dataclasses.dataclass(frozen=True)
class CoidaInput:
    calculated_for: datetime.date
    assessment_period_start: datetime.date
    assessment_period_end: datetime.date
    #: "the maximum amount of earnings on which an assessment of an employer
    #: shall be calculated", per employee per annum (COIDA s83(8)).
    annual_ceiling: StatutoryFigure
    earnings: Sequence[CoidaEarning]


@dataclasses.dataclass(frozen=True)
class CoidaResult:
    #: Every flagged line, summed — before the cap.
    earnings: Money
    #: Lines whose component is outside the base. Reported so the return can be
    #: reconciled against the payroll total, never declared.
    excluded: Money
    #: What goes on the return for this employee: the earnings, capped.
    declared: Money
    capped: bool
    trace: CalculationTrace


def line_as_text(line: CoidaEarning) -> str:
    """One line as the trace records it, so the figure replays from the trace
    alone (D-284)."""
    return f"{line.source_code}|{line.amount}|{line.is_coida_base}"


def assessment_earnings(data: CoidaInput) -> CoidaResult:
    """One employee's declarable earnings for one assessment period."""
    if data.assessment_period_end < data.assessment_period_start:
        raise CoidaInputError(
            f"The assessment period ends on {data.assessment_period_end:%d %B %Y}, before it "
            f"starts on {data.assessment_period_start:%d %B %Y}."
        )

    warnings: list[str] = []
    earnings = sum((line.amount for line in data.earnings if line.is_coida_base), ZERO)
    excluded = sum((line.amount for line in data.earnings if not line.is_coida_base), ZERO)

    declarable = earnings
    if earnings < ZERO:
        # Reversals net off because their lines are negative, so a negative
        # total means a reversal with nothing in the period to reverse against —
        # the original was declared in an earlier period. Nothing to declare
        # here, and it is said rather than silently floored.
        warnings.append(
            f"COIDA earnings for the period total {earnings}: a reversal outweighs what was "
            f"paid. Nothing is declared for this employee; the correction belongs to the "
            f"period of the original, which may need a revised return."
        )
        declarable = ZERO

    ceiling = data.annual_ceiling.value
    capped = declarable > ceiling
    declared = ceiling if capped else declarable

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            assessment_period_start=data.assessment_period_start,
            assessment_period_end=data.assessment_period_end,
            annual_ceiling=ceiling,
        )
        | {f"line_{index:03d}": line_as_text(line) for index, line in enumerate(data.earnings, 1)},
        statutory_rows=rows_of(data.annual_ceiling),
        outputs=as_text(
            earnings=Money.of(earnings).exact,
            excluded=Money.of(excluded).exact,
            declared=Money.of(declared).exact,
            capped=capped,
        ),
        warnings=tuple(warnings),
    )

    return CoidaResult(
        earnings=Money.of(earnings),
        excluded=Money.of(excluded),
        declared=Money.of(declared),
        capped=capped,
        trace=trace,
    )
