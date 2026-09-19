"""UIF contributions — Unemployment Insurance Contributions Act 4 of 2002.

**The golden anchor, published by SARS** ("Unemployment Insurance Fund ceiling
earnings", 3 August 2021, citing Government Gazette 44641 of 28 May 2021):

    "As from 1 June 2021, the maximum earnings ceiling for the Unemployment
    Insurance Fund (UIF) is R17 712 per month or R212 544 annually. For
    employees who earn more than this amount, the contribution is calculated
    using the maximum earnings ceiling amount. Therefore the maximum
    contribution which can be deducted, for employees who earn more than
    R17 712 per month, is R177,12 per month."

That last figure is the golden one: R177,12. It is the only published COMPUTED
UIF figure I could find — SARS's UIF guide (UIF-GEN-01-G01), its SDL guide and
its 2026 PAYE employer guide all give the method and the ceiling table but work
no example. Everything else here is tested against the words of the Act rather
than against a published arithmetic result, and D-210 records that.

The statutory words each case tests:

- s6(1)(a)(i) and (ii): one per cent, employee and employer, separately
- s6(2): the cap applies "to so much of the remuneration ... as exceeds" the
  determined amount — so AT the ceiling nothing is excluded
- s1 "remuneration" ... "does not include ... (c) by way of commission"
- s4(1)(a): an employee employed for less than 24 hours a month, and that
  employer, are outside the Act — taken as a declared input (D-110), never
  computed from hours here
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import Money, StatutoryFigure
from calculators.uif import UifInput, contribution

MARCH = datetime.date(2026, 3, 31)

CEILING = StatutoryFigure(
    value=Decimal("17712.00"),
    table="statutory_parameter",
    row_id=901,
    description="UIF_MONTHLY_CEILING, GG 44641 of 28 May 2021",
)
RATE = StatutoryFigure(
    value=Decimal("1.000000"),
    table="statutory_parameter",
    row_id=902,
    description="UIF_EMPLOYEE_RATE_PCT, UICA s6(1)(a)(i)",
)
EMPLOYER_RATE = StatutoryFigure(
    value=Decimal("1.000000"),
    table="statutory_parameter",
    row_id=903,
    description="UIF_EMPLOYER_RATE_PCT, UICA s6(1)(a)(ii)",
)


def an_input(**overrides) -> UifInput:
    values = {
        "calculated_for": MARCH,
        "remuneration": Decimal("10000.00"),
        "commission": Decimal("0.00"),
        "excluded_remuneration": Decimal("0.00"),
        "monthly_ceiling": CEILING,
        "employee_rate_percent": RATE,
        "employer_rate_percent": EMPLOYER_RATE,
        "is_exempt": False,
        "exemption_reason": "",
    }
    values.update(overrides)
    return UifInput(**values)


# ----------------------------------------------------------------- the golden


@pytest.mark.golden
def test_sars_published_maximum_contribution_of_r177_12():
    """SARS, 3 August 2021: "the maximum contribution which can be deducted, for
    employees who earn more than R17 712 per month, is R177,12 per month"."""
    result = contribution(an_input(remuneration=Decimal("25000.00")))

    assert result.employee.rounded == Decimal("177.12")
    assert result.employer.rounded == Decimal("177.12")


@pytest.mark.golden
def test_the_published_maximum_is_reached_exactly_at_the_ceiling_too():
    """s6(2) caps "so much of the remuneration ... as exceeds" the amount, so at
    the ceiling there is no excess and the whole of it is contributable — the
    same R177,12 SARS publishes for earnings above it."""
    result = contribution(an_input(remuneration=Decimal("17712.00")))

    assert result.contribution_base.rounded == Decimal("17712.00")
    assert result.employee.rounded == Decimal("177.12")


# ------------------------------------------------------------- the boundary


def test_below_the_ceiling_the_whole_remuneration_is_contributable():
    result = contribution(an_input(remuneration=Decimal("10000.00")))
    assert result.contribution_base.rounded == Decimal("10000.00")
    assert result.employee.rounded == Decimal("100.00")
    assert result.employer.rounded == Decimal("100.00")


def test_one_cent_above_the_ceiling_is_capped():
    """The exact boundary, tested from both sides (D-158's standing lesson)."""
    at = contribution(an_input(remuneration=Decimal("17712.00")))
    above = contribution(an_input(remuneration=Decimal("17712.01")))

    assert at.contribution_base.exact == Decimal("17712.000000")
    assert above.contribution_base.exact == Decimal("17712.000000")
    assert above.employee.rounded == at.employee.rounded == Decimal("177.12")
    assert at.capped is False, "at the ceiling nothing EXCEEDS it, so nothing is excluded"
    assert above.capped is True


# --------------------------------------------------------- what is in the base


def test_commission_is_excluded_from_the_base():
    """UICA s1: "remuneration" ... "does not include any amount paid or payable
    to an employee ... (c) by way of commission"."""
    result = contribution(an_input(remuneration=Decimal("12000.00"), commission=Decimal("2000.00")))

    assert result.contribution_base.rounded == Decimal("10000.00")
    assert result.employee.rounded == Decimal("100.00")


def test_a_bonus_is_not_excluded():
    """A bonus is remuneration under paragraph 1 of the Fourth Schedule and is
    not in s1's exclusion list — so it contributes, unlike commission. The
    difference is the one this build's own notes single out."""
    plain = contribution(an_input(remuneration=Decimal("10000.00")))
    with_bonus = contribution(an_input(remuneration=Decimal("15000.00")))

    assert with_bonus.contribution_base.rounded == Decimal("15000.00")
    assert with_bonus.employee.rounded == Decimal("150.00")
    assert with_bonus.employee.rounded > plain.employee.rounded


def test_a_bonus_that_carries_remuneration_over_the_ceiling_is_capped_not_excluded():
    """The two rules compose: the bonus counts, and then the ceiling bites."""
    result = contribution(an_input(remuneration=Decimal("20000.00")))
    assert result.contribution_base.rounded == Decimal("17712.00")
    assert result.employee.rounded == Decimal("177.12")


def test_pension_and_the_other_section_1_exclusions_come_out_too():
    result = contribution(
        an_input(remuneration=Decimal("11000.00"), excluded_remuneration=Decimal("1000.00"))
    )
    assert result.contribution_base.rounded == Decimal("10000.00")


# ------------------------------------------------------------- the exemption


def test_an_exempt_employee_contributes_nothing_and_neither_does_the_employer():
    """UICA s4(1)(a): an employee employed for less than 24 hours a month, and
    that employer, are outside the Act. A cleaner engaged for two half-days a
    month is under it, so this is not an edge case in this product."""
    result = contribution(
        an_input(
            remuneration=Decimal("900.00"),
            is_exempt=True,
            exemption_reason="Employed for less than 24 hours a month (UICA s4(1)(a))",
        )
    )

    assert result.employee == Money.of(Decimal("0"))
    assert result.employer == Money.of(Decimal("0"))
    assert result.contribution_base.exact == Decimal("0.000000")
    assert "s4(1)(a)" in result.trace.outputs["exemption_reason"]


def test_the_exemption_is_declared_and_never_computed_from_hours():
    """D-110: the employer states which side of the line the employee is on and
    the statute keeps the number. There is nowhere to hand this calculator an
    hours figure, so it cannot decide the question itself."""
    fields = set(UifInput.__dataclass_fields__)
    assert "is_exempt" in fields
    assert not {f for f in fields if "hour" in f}, (
        f"UifInput takes hours: {sorted(f for f in fields if 'hour' in f)}. The 24-hour "
        "question is a declared boolean (D-110), not one this calculator answers."
    )


# ------------------------------------------------------------------ the trace


def test_the_result_carries_the_rows_it_was_computed_from():
    """Invariant 5, by construction: the keys of every statutory row handed in."""
    result = contribution(an_input())

    assert result.trace.calculator == "uif.contribution"
    assert result.trace.calculated_for == MARCH
    assert ("statutory_parameter", 901) in result.trace.statutory_rows
    assert ("statutory_parameter", 902) in result.trace.statutory_rows
    assert ("statutory_parameter", 903) in result.trace.statutory_rows
    assert result.trace.outputs["employee"] == "100.000000"


def test_every_figure_is_returned_unrounded_as_well_as_rounded():
    """Invariant 6: a chain of individually rounded steps does not add up, so
    the unrounded figure travels with the rounded one."""
    result = contribution(an_input(remuneration=Decimal("3333.33")))

    assert result.employee.exact == Decimal("33.333300")
    assert result.employee.rounded == Decimal("33.33")
    assert result.employer.exact == Decimal("33.333300")


def test_a_zero_remuneration_period_still_produces_a_trace():
    """A trace is written even for a zero, because a missing trace row must mean
    "this never ran" and nothing else (D-208)."""
    result = contribution(an_input(remuneration=Decimal("0.00")))
    assert result.employee.rounded == Decimal("0.00")
    assert result.trace.statutory_rows


def test_exclusions_larger_than_the_remuneration_contribute_nothing_and_say_so():
    """Not an arithmetic guard: it means the caller captured exclusions bigger
    than the remuneration they came out of. Contribute nothing, and warn —
    silently flooring it would hide a capture error upstream."""
    result = contribution(an_input(remuneration=Decimal("1000.00"), commission=Decimal("1500.00")))

    assert result.contribution_base.exact == Decimal("0.000000")
    assert result.employee.rounded == Decimal("0.00")
    assert any("exceed" in warning for warning in result.trace.warnings), result.trace.warnings


def test_an_exemption_with_no_reason_recorded_warns():
    """UICA s4 exempts by CATEGORY, and which category is what an auditor asks
    for a year later."""
    result = contribution(an_input(is_exempt=True, exemption_reason=""))

    assert result.employee.rounded == Decimal("0.00")
    assert any("no reason" in w for w in result.trace.warnings), result.trace.warnings
