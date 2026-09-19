"""SDL — Skills Development Levies Act 9 of 1999.

Pure, like every calculator here: the rate arrives as a ``StatutoryFigure``
carrying the key of the row the caller read, and the date arrives as an input.

**s3(1)** — "Every employer must pay a skills development levy ... (a)(ii) from
1 April 2001, at a rate of one per cent of the leviable amount", or at such a
rate as the Minister announces in the budget under (b). **s3(3)** — the leviable
amount is "the total amount of remuneration, paid or payable ... to its
employees during any month, as determined in accordance with the provisions of
the Fourth Schedule to the Income Tax Act".

**s4(b) is the whole point of this module's shape.** The levy is not payable by
an employer where "during any month, there are reasonable grounds for believing
that the total amount of remuneration ... paid or payable by that employer to
all its employees during the FOLLOWING 12 month period will not exceed
R500 000".

That is a forward-looking belief, and it cannot be computed from history. Two
employers with identical payrolls to date are on opposite sides of it if one is
hiring and the other is winding down. So liability is a BOOLEAN INPUT, set by a
human (D-209), and this function takes no payroll history, no threshold and no
twelve-month total — there is nowhere to hand it one. The R500 000 threshold is
still loaded reference data, because the human deciding needs to be shown it,
but it is an input to the SCREEN, not to this calculator.

**No sector exclusion for domestic employers.** The Act names five exemptions in
s4 — public service, the s4(b) threshold, certain public benefit organisations,
mostly-Parliament-funded public entities, and exempted municipalities. A private
household employing a domestic worker is not among them: it is caught by s3(1)
like any employer and reaches the same answer through s4(b), because its payroll
is nowhere near R500 000. There is nothing sector-specific to load and nothing
to branch on here.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from calculators.base import ZERO, CalculationTrace, Money, StatutoryFigure, as_text, rows_of

CALCULATOR = "sdl.levy"

PER_CENT = Decimal("100")


@dataclasses.dataclass(frozen=True)
class SdlInput:
    """One employer, one month. No history: see s4(b) in the module docstring."""

    calculated_for: datetime.date
    #: s3(3): the month's total remuneration as determined under the Fourth
    #: Schedule, after s3(4)'s exclusions. The caller assembles it.
    leviable_amount: Decimal
    rate_percent: StatutoryFigure
    #: s4: whether this employer pays the levy at all. DECLARED by a human, for
    #: the forward-looking reason in the docstring - never inferred here.
    employer_is_liable: bool
    #: Which s4 paragraph, or the employer's own reasoning, for the audit trail.
    exemption_reason: str = ""


@dataclasses.dataclass(frozen=True)
class SdlResult:
    levy: Money
    leviable_amount: Money
    trace: CalculationTrace


def levy(data: SdlInput) -> SdlResult:
    """The month's skills development levy."""
    warnings: list[str] = []

    leviable = data.leviable_amount
    if leviable < ZERO:
        warnings.append(
            f"A negative leviable amount ({leviable}) was handed in; treated as nil. "
            f"A correction belongs in the month it corrects, not as a negative levy."
        )
        leviable = ZERO

    if data.employer_is_liable:
        payable = leviable * data.rate_percent.value / PER_CENT
    else:
        payable = ZERO
        if not data.exemption_reason:
            warnings.append(
                "Not liable for SDL with no reason recorded. s4 exempts by category, and "
                "which one - usually s4(b)'s R500 000 belief - is what an auditor asks for."
            )

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            leviable_amount=data.leviable_amount,
            rate_percent=data.rate_percent.value,
            employer_is_liable=data.employer_is_liable,
        ),
        statutory_rows=rows_of(data.rate_percent),
        outputs=as_text(
            levy=Money.of(payable).exact,
            employer_is_liable=data.employer_is_liable,
            exemption_reason=data.exemption_reason,
        ),
        warnings=tuple(warnings),
    )

    return SdlResult(
        levy=Money.of(payable),
        leviable_amount=Money.of(leviable),
        trace=trace,
    )
