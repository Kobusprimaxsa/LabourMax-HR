"""The shape every calculator in this system has. Settled with two to test it
against, rather than after eight have each grown their own (D-207).

A calculator is a module-level function taking ONE frozen input structure and
returning ONE frozen result. It may not import ``django.db``, touch the
filesystem, call ``datetime.now()`` or ``date.today()``, or read a setting.
Every date it needs — including "the date this calculation is for" — arrives as
an input, because a calculator that asks what today is will give a different
answer when March 2026 is re-run in 2029. The rule is enforced by
``calculators/tests/test_contract.py``, which reads every module's imports; a
rule with no test is the seventh silent guard this build has shipped.

**Provenance travels with the figure, and it is the KEY, not the citation.**
``StatutoryFigure`` carries the table and primary key of the row the caller
read. The caller reads rows; the calculator records which ones it was handed.
Invariant 5 is then satisfied by construction rather than by the caller
remembering to write it down — and the trace can be resolved back to the exact
rows eighteen months later, which a copied citation string cannot do.

**Rounding happens in one place and nowhere else** (invariant 6). ``Money``
carries both figures: ``exact``, at six decimal places, and ``rounded``, at two,
ROUND_HALF_UP. A calculator returns Money, never a bare rounded Decimal — a
chain of individually rounded steps does not add up, and rounding early destroys
information the next calculator needs. Only the payslip line reads ``.rounded``.
"""

from __future__ import annotations

import dataclasses
import datetime
import typing
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

#: Intermediate precision. Six places, per invariant 6's "4-6 decimal places".
EXACT = Decimal("0.000001")
#: The payslip line's precision, and the only place a figure is rounded.
CENTS = Decimal("0.01")

ZERO = Decimal("0")


class Sourced(typing.Protocol):
    """Anything carrying the key of the reference row it came from.

    ``StatutoryFigure`` is the usual case: one figure, one row. But a PAYE
    bracket is four numbers off ONE row — a lower bound, an upper bound, a
    cumulative base and a marginal rate — and splitting it into four
    ``StatutoryFigure``s repeating the same key four times makes the caller's
    code unreadable without recording anything more. So provenance is a
    protocol: ``rows_of()`` takes anything that can say which row it came from,
    and a multi-column structure carries the pair once.
    """

    table: str
    row_id: int


@dataclasses.dataclass(frozen=True)
class StatutoryFigure:
    """One figure read from reference data, with the key of the row it came from.

    ``table`` and ``row_id`` are what the trace stores. A citation string is for
    a human reading a refusal; the key is what lets somebody in 2029 open the
    exact row a 2026 payslip was computed against, including if the citation text
    has since been corrected (which it has, twice).
    """

    value: Decimal
    table: str
    row_id: int
    description: str = ""

    def __post_init__(self):
        if not isinstance(self.value, Decimal):
            raise TypeError(
                f"{self.table}.{self.row_id} carries {type(self.value).__name__}, not Decimal. "
                "Money is Decimal everywhere in this system (invariant 6)."
            )


@dataclasses.dataclass(frozen=True)
class Money:
    """An amount, unrounded and rounded, together.

    Both are kept because invariant 6 says so: the unrounded figure is what the
    next calculation uses and what is stored alongside the rounded one; the
    rounded figure is what the payslip line shows.
    """

    exact: Decimal
    rounded: Decimal

    @classmethod
    def of(cls, value: Decimal) -> Money:
        exact = value.quantize(EXACT, rounding=ROUND_HALF_UP)
        return cls(exact=exact, rounded=exact.quantize(CENTS, rounding=ROUND_HALF_UP))

    def __str__(self) -> str:
        return f"{self.rounded}"


@dataclasses.dataclass(frozen=True)
class CalculationTrace:
    """What a calculator did, as data. Invariant 5.

    The calculator PRODUCES this; the caller PERSISTS it (D-208). Nothing here
    knows about a payslip, a run or a database — which is exactly why the same
    structure can be written by a payroll run, replayed in a test, or printed in
    a dispute eighteen months from now.

    ``statutory_rows`` is the set of (table, row_id) pairs the calculation read,
    collected from the ``StatutoryFigure``s it was given rather than listed by
    hand, so a figure cannot be used without being recorded.
    """

    calculator: str
    calculated_for: datetime.date
    inputs: Mapping[str, str]
    statutory_rows: Sequence[tuple[str, int]]
    outputs: Mapping[str, str]
    warnings: Sequence[str] = ()


def rows_of(*figures: Sourced) -> tuple[tuple[str, int], ...]:
    """The provenance pairs of every figure handed in, de-duplicated, ordered."""
    seen = {(figure.table, figure.row_id) for figure in figures}
    return tuple(sorted(seen))


def as_text(**values) -> dict[str, str]:
    """Inputs and outputs as strings, so a trace row is JSON the day it is written
    and reads the same in 2029 as it does now. Decimals keep their own repr —
    never a float, which would round on the way in."""
    return {name: ("" if value is None else str(value)) for name, value in values.items()}
