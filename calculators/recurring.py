"""Recurring earnings and deductions — the lines that repeat every period (P7 chunk 8c).

A transport allowance, an accommodation deduction, an advance being paid back.
Each arrives as one ``employee_recurring_component`` row the caller has already
read and checked, and leaves as one priced line. Pure, like every calculator
here: the basic it is a percentage of, the ceiling it may not pass and what is
still owed on a loan all arrive as inputs.

**BCEA s34 as written, and nothing added** (D-301). s34(1) forbids a deduction
without the employee's written agreement "in respect of a debt specified in the
agreement" unless a law, collective agreement, court order or arbitration award
requires it — that is a CONSENT rule, and the caller checks it, because consent
is a file on record and not a figure. The Act states NO ceiling on deductions in
total. The one percentage in the section, s34(2)(d)'s "may not exceed
one-quarter of the employee's remuneration in money", governs a deduction to
recover LOSS OR DAMAGE and nothing else; no loss-or-damage component exists in
this build, and when one does its quarter is a cited reference row, never a
figure here. What this module does enforce is narrower and each is somebody
else's figure:

* SD7's accommodation ceiling — ``working_time_rule_set.accommodation_deduction_max_pct``,
  handed in with the key of its row. A line above it REFUSES: the percentage
  was captured against the law, and silently deducting less would hide that.
* ``total_deduction_cap_pct`` — a ceiling the employer set on ONE line. That one
  CLAMPS, with a note: deducting less than the employer allowed is always
  lawful, and it is the whole point of the column.
* What is still owed — a loan's last instalment is the remainder, never the
  full instalment, and a loan paid off produces no line at all.

There is no golden-file test for this module, and that is permanent (D-150's
position): no regulator publishes a worked recurring-deduction example. The
figures are percentages and minimums; they are pinned by table-driven tests.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from decimal import Decimal

from calculators.base import ZERO, CalculationTrace, Money, as_text

CALCULATOR = "recurring.recurring_lines"

PER_CENT = Decimal("100")


class RecurringRefusedError(ValueError):
    """A recurring line cannot lawfully be priced as captured."""


class LineKind(enum.StrEnum):
    EARNING = "earning"
    DEDUCTION = "deduction"


@dataclasses.dataclass(frozen=True)
class AccommodationCeiling:
    """What the instrument in force says about an accommodation deduction.

    Sourced (``base.Sourced``): the two figures come off one rule set row.
    ``capped`` FALSE is the BCEA's position — no percentage stated — and is the
    ABSENCE of a ceiling, never a ceiling of zero (D-198).
    """

    capped: bool
    max_percent: Decimal | None
    table: str
    row_id: int


@dataclasses.dataclass(frozen=True)
class RecurringLine:
    """One ``employee_recurring_component`` row, as the caller read it."""

    line_id: int
    component_code: str
    description: str
    kind: LineKind
    #: Exactly one of these two is set; the row's CHECK and clean() hold that.
    amount: Decimal | None = None
    percentage_of_basic: Decimal | None = None
    #: The employer's own ceiling on THIS line, as a percentage of basic.
    cap_percent: Decimal | None = None
    #: A loan's principal less what finalised payslips have recovered. None
    #: means the line runs no balance.
    owed: Decimal | None = None
    #: The accommodation deduction, which SD7 caps by statute.
    is_accommodation: bool = False


@dataclasses.dataclass(frozen=True)
class RecurringInput:
    calculated_for: datetime.date
    #: The period's BASIC as priced by ``gross.gross_pay`` — what
    #: ``percentage_of_basic`` and every ceiling are a percentage OF.
    basic: Decimal
    lines: tuple[RecurringLine, ...]
    #: Needed only when an accommodation line is present.
    accommodation_ceiling: AccommodationCeiling | None = None


@dataclasses.dataclass(frozen=True)
class PricedLine:
    line_id: int
    component_code: str
    kind: LineKind
    description: str
    amount: Money
    percentage: Decimal | None
    note: str


@dataclasses.dataclass(frozen=True)
class RecurringResult:
    earnings: tuple[PricedLine, ...]
    deductions: tuple[PricedLine, ...]
    trace: CalculationTrace


def _of_basic(basic: Decimal, percent: Decimal) -> Decimal:
    return basic * percent / PER_CENT


def recurring_lines(data: RecurringInput) -> RecurringResult:
    """Every recurring line for one employee, one period."""
    warnings: list[str] = []
    priced: list[PricedLine] = []
    inputs: dict[str, object] = {"basic": data.basic}
    outputs: dict[str, object] = {}
    rows: list[tuple[str, int]] = []

    for line in data.lines:
        key = f"{line.component_code}_{line.line_id}"
        inputs[key] = (
            f"{line.kind.value}; amount={line.amount}; percentage_of_basic="
            f"{line.percentage_of_basic}; cap_percent={line.cap_percent}; owed={line.owed}; "
            f"accommodation={line.is_accommodation}"
        )
        notes: list[str] = []

        if line.amount is not None:
            figure = line.amount
        else:
            figure = _of_basic(data.basic, line.percentage_of_basic)
            notes.append(f"{line.percentage_of_basic}% of basic {data.basic}")

        if line.is_accommodation:
            ceiling = data.accommodation_ceiling
            if ceiling is None:
                raise RecurringRefusedError(
                    f"{line.component_code}: no working time rule set was handed in, so the "
                    f"accommodation ceiling cannot be checked."
                )
            rows.append((ceiling.table, ceiling.row_id))
            if ceiling.capped and figure > _of_basic(data.basic, ceiling.max_percent):
                raise RecurringRefusedError(
                    f"{line.component_code}: {Money.of(figure)} is more than the "
                    f"{ceiling.max_percent}% of the wage the instrument in force permits "
                    f"for accommodation ({Money.of(_of_basic(data.basic, ceiling.max_percent))} "
                    f"of basic {data.basic}). Correct the line's percentage."
                )

        if line.kind is LineKind.DEDUCTION and line.cap_percent is not None:
            limit = _of_basic(data.basic, line.cap_percent)
            if figure > limit:
                notes.append(f"held to the line's own {line.cap_percent}% ceiling")
                warnings.append(
                    f"{key}: {Money.of(figure)} held to {Money.of(limit)}, the line's own "
                    f"{line.cap_percent}% of basic ceiling."
                )
                figure = limit

        if line.owed is not None:
            if line.owed <= ZERO:
                outputs[key] = "nothing owed"
                continue
            if figure > line.owed:
                notes.append(f"final instalment: {line.owed} was owed")
                figure = line.owed

        outputs[key] = Money.of(figure).exact
        if not figure:
            continue
        priced.append(
            PricedLine(
                line_id=line.line_id,
                component_code=line.component_code,
                kind=line.kind,
                description=line.description,
                amount=Money.of(figure),
                percentage=line.percentage_of_basic,
                note="; ".join(notes),
            )
        )

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(**inputs),
        statutory_rows=tuple(sorted(set(rows))),
        outputs=as_text(**outputs),
        warnings=tuple(warnings),
    )
    return RecurringResult(
        earnings=tuple(p for p in priced if p.kind is LineKind.EARNING),
        deductions=tuple(p for p in priced if p.kind is LineKind.DEDUCTION),
        trace=trace,
    )
