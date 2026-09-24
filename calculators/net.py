"""Net pay: earnings less deductions, and the refusal when they do not fit (D-312).

Pure. Every figure arrives rounded, because net pay is what the payslip lines
add up to and the lines are where rounding happens (invariant 6).

**Net pay is never below zero, and nothing here clamps it there.** Sheet 03
forbids a negative net on an ordinary payslip. When the deductions do not fit,
the lawful answer is not for this module to choose which deduction yields:

* PAYE and UIF never do the damage on their own — PAYE never exceeds the taxable
  income and UIF is a percentage of it (D-284's properties) — so a negative net
  means a recurring deduction the employee agreed to, at an amount they agreed
  to, cannot be taken in full this period;
* which one gives way is a choice with consequences — an advance repaid slower
  runs longer, an accommodation deduction not taken is forgone — and the
  employee's s34 consent was to specific amounts, not to whatever fits;
* and silently holding one back would leave the payslip looking right while an
  agreed deduction quietly did not happen.

So the payslip is REFUSED, naming every deduction and its amount, and the refusal
becomes a blocking issue on that employee (D-292) for a person to settle —
suspend or reduce a line for the period by closing its row and capturing
another, or set the line's own ceiling (D-301). Everybody else on the run is paid.
"""

from __future__ import annotations

import dataclasses
import datetime

from calculators.base import ZERO, CalculationTrace, Money, as_text

CALCULATOR = "net.net_pay"


class NetPayRefusedError(ValueError):
    """The deductions exceed the earnings. Carries what the refusal names."""

    def __init__(self, message: str, *, shortfall: Money, deductions: tuple):
        super().__init__(message)
        self.shortfall = shortfall
        self.deductions = deductions


@dataclasses.dataclass(frozen=True)
class Deduction:
    component_code: str
    amount: Money
    #: PAYE and UIF: computed from reference data, never agreed to.
    is_statutory: bool


@dataclasses.dataclass(frozen=True)
class NetInput:
    calculated_for: datetime.date
    #: The rounded earnings lines.
    earnings: tuple[Money, ...]
    deductions: tuple[Deduction, ...]


@dataclasses.dataclass(frozen=True)
class NetResult:
    total_earnings: Money
    total_deductions: Money
    net: Money
    trace: CalculationTrace


def net_pay(data: NetInput) -> NetResult:
    earnings = sum((line.rounded for line in data.earnings), ZERO)
    deductions = sum((line.amount.rounded for line in data.deductions), ZERO)
    net = earnings - deductions
    if net < ZERO:
        named = ", ".join(f"{line.component_code} {line.amount}" for line in data.deductions)
        raise NetPayRefusedError(
            f"Deductions of {Money.of(deductions)} exceed earnings of {Money.of(earnings)} by "
            f"{Money.of(-net)} ({named}). A negative net is never stored (sheet 03), and no "
            f"deduction is quietly held back: which one gives way is a decision about what the "
            f"employee agreed to. Reduce or suspend a recurring line for this period.",
            shortfall=Money.of(-net),
            deductions=data.deductions,
        )
    return NetResult(
        total_earnings=Money.of(earnings),
        total_deductions=Money.of(deductions),
        net=Money.of(net),
        trace=CalculationTrace(
            calculator=CALCULATOR,
            calculated_for=data.calculated_for,
            inputs=as_text(
                earnings=earnings,
                **{
                    f"{line.component_code}_{index}": line.amount.rounded
                    for index, line in enumerate(data.deductions)
                },
            ),
            statutory_rows=(),
            outputs=as_text(total_deductions=deductions, net=net),
        ),
    )
