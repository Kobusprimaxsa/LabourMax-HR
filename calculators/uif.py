"""UIF contributions — Unemployment Insurance Contributions Act 4 of 2002.

Pure. Every figure arrives as a ``StatutoryFigure`` carrying the key of the row
the caller read, and every date arrives as an input.

**s6(1)(a)** — one per cent of the remuneration paid or payable during the
month, by the employee under (i) and by the employer under (ii). Two outputs,
never one doubled: the EMP201 declares them separately, and the Minister may
move one without the other (s6(1)(b) lets a percentage be announced in the
budget).

**s6(2) — the ceiling, and which side the boundary falls on.** "Subsection (1)
does not apply to so much of the remuneration paid or payable by an employer to
an employee during any month **as exceeds** an amount determined from time to
time by the Minister of Finance by notice in the Gazette." The exclusion bites
only on the EXCESS, so remuneration exactly at the ceiling is contributable in
full: the base is ``min(remuneration, ceiling)``. SARS publishes the resulting
figure — R177,12 a month at the R17 712 ceiling — and the golden test holds this
module to it.

The ceiling moves **on ministerial notice, on no fixed calendar**, independent of
the tax year: "determined from time to time ... by notice in the Gazette". It is
the parameter this build's notes single out as the most often missed. Nothing
here knows its value or its effective date; both belong to the row the caller
resolved.

**s1 "remuneration"** is the Fourth Schedule's definition "but does not include
any amount paid or payable to an employee — (a) by way of any pension,
superannuation allowance or retiring allowance; (b) which constitutes an amount
contemplated in paragraphs (a), (cA), (d), (e) or (eA) of the definition of
'gross income'...; or **(c) by way of commission**". Commission is out; a bonus
is not in that list and is therefore in.

**s4(1)(a)** puts an employee employed for less than 24 hours a month, and that
employer, outside the Act. It arrives as a DECLARED boolean with its reason
(D-110): a cleaner engaged for two half-days a month is under that line, and
whether she is is a fact the employer states, not one this function derives from
an hours figure it has no business holding.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from calculators.base import (
    ZERO,
    CalculationTrace,
    Money,
    StatutoryFigure,
    as_text,
    rows_of,
)

CALCULATOR = "uif.contribution"

#: Rates are stored as percentages (1.000000 meaning one per cent), so the
#: divisor is arithmetic, not a statutory figure — the Act's "one per cent" is
#: the ROW's value, never a literal here.
PER_CENT = Decimal("100")


@dataclasses.dataclass(frozen=True)
class UifInput:
    """One employee, one month. Frozen, Decimal, and no clock."""

    calculated_for: datetime.date
    #: Remuneration as defined in paragraph 1 of the Fourth Schedule, before the
    #: s1 exclusions below are taken off. A bonus is part of this.
    remuneration: Decimal
    #: s1(c). Split out because it is the exclusion this product gets asked about.
    commission: Decimal
    #: s1(a) and (b) — pension, superannuation and retiring allowances, and the
    #: gross-income paragraphs. One field: they are excluded on the same footing.
    excluded_remuneration: Decimal
    monthly_ceiling: StatutoryFigure
    employee_rate_percent: StatutoryFigure
    employer_rate_percent: StatutoryFigure
    #: s4(1)(a) and the other s4 exclusions, DECLARED (D-110).
    is_exempt: bool = False
    exemption_reason: str = ""


@dataclasses.dataclass(frozen=True)
class UifResult:
    employee: Money
    employer: Money
    contribution_base: Money
    excluded_total: Money
    capped: bool
    trace: CalculationTrace


def contribution(data: UifInput) -> UifResult:
    """The employee's and the employer's contribution for one month."""
    warnings: list[str] = []

    excluded = data.commission + data.excluded_remuneration
    remuneration_for_uif = data.remuneration - excluded
    if remuneration_for_uif < ZERO:
        # Not an arithmetic guard: it means the caller handed in exclusions
        # larger than the remuneration they came out of, which is a capture
        # error upstream. Contribute nothing and SAY so, rather than computing a
        # negative contribution or silently flooring it.
        warnings.append(
            f"Exclusions ({excluded}) exceed remuneration ({data.remuneration}); "
            f"the contribution base is treated as nil. Check what was captured."
        )
        remuneration_for_uif = ZERO

    ceiling = data.monthly_ceiling.value
    capped = remuneration_for_uif > ceiling
    base = ceiling if capped else remuneration_for_uif

    if data.is_exempt:
        base = ZERO
        if not data.exemption_reason:
            # The reason is what makes the exemption auditable a year later.
            warnings.append(
                "Exempt from UIF with no reason recorded. UICA s4 exempts by "
                "category, and which category is a fact the employer declared."
            )

    employee = Money.of(base * data.employee_rate_percent.value / PER_CENT)
    employer = Money.of(base * data.employer_rate_percent.value / PER_CENT)

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            remuneration=data.remuneration,
            commission=data.commission,
            excluded_remuneration=data.excluded_remuneration,
            monthly_ceiling=ceiling,
            employee_rate_percent=data.employee_rate_percent.value,
            employer_rate_percent=data.employer_rate_percent.value,
            is_exempt=data.is_exempt,
            # An INPUT: it is what the employer declared. It is also echoed in
            # the outputs, where stored traces have always carried it; a trace
            # that held it only there could not be replayed (D-284).
            exemption_reason=data.exemption_reason,
        ),
        statutory_rows=rows_of(
            data.monthly_ceiling, data.employee_rate_percent, data.employer_rate_percent
        ),
        outputs=as_text(
            contribution_base=Money.of(base).exact,
            employee=Money.of(base * data.employee_rate_percent.value / PER_CENT).exact,
            employer=Money.of(base * data.employer_rate_percent.value / PER_CENT).exact,
            capped=capped,
            exemption_reason=data.exemption_reason,
        ),
        warnings=tuple(warnings),
    )

    return UifResult(
        employee=employee,
        employer=employer,
        contribution_base=Money.of(base),
        excluded_total=Money.of(excluded),
        capped=capped,
        trace=trace,
    )
