"""COIDA accumulation against the Department's own worked examples.

**The golden source is DEL-published**, and it is the only worked example the
Department publishes for anything this build computes (D-283): the capping
examples printed in the notices that set the maximum earnings.

* GN 2390 of 2024, GG 50386, 27 March 2024, page 5 — "Maximum earnings R568 959
  [2023, 01 March 2023 to 29 February 2024] … 1. If an employee has earned a
  total earnings of R600 000.00 from the employer during the period as stated
  above, the amount should be capped at R568 959.00 and be declared as such.
  2. if an employee has earned total earnings of any amount below R568 959.00,
  the total earnings must be declared as is, regardless of whether the said
  employee worked for a full year or part year."
  https://www.gov.za/sites/default/files/gcis_document/202403/50386gen2390.pdf
* GN 1723 of 2023, GG 48337, 30 March 2023 — the same two examples on the 2022
  season's R529 264: "Full annual maximum earnings of R529 264.00 will apply
  irrespective of the number of months the employee was employed".
  https://www.gov.za/sites/default/files/gcis_document/202303/48337gen1723.pdf

Both ceilings are the ones the examples were worked on, passed in here as
literals the way every golden test passes its figures. Neither is loaded, and
neither needs to be: the 2026 figure, R668 000 under Notice 3910 of 2026, is.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.coida import CoidaEarning, CoidaInput, CoidaInputError, assessment_earnings

SEASON_2023_START = datetime.date(2023, 3, 1)
SEASON_2023_END = datetime.date(2024, 2, 29)

CEILING_2023 = StatutoryFigure(
    value=Decimal("568959.00"),
    table="statutory_parameter",
    row_id=2023,
    description="COIDA maximum earnings, 2023 season, GN 2390 of 2024",
)
CEILING_2022 = StatutoryFigure(
    value=Decimal("529264.00"),
    table="statutory_parameter",
    row_id=2022,
    description="COIDA maximum earnings, 2022 season, GN 1723 of 2023",
)


def earned(*amounts, flagged=True, code="3601"):
    return tuple(CoidaEarning(code, Decimal(amount), flagged) for amount in amounts)


def an_input(earnings, ceiling=CEILING_2023, **overrides) -> CoidaInput:
    values = {
        "calculated_for": SEASON_2023_END,
        "assessment_period_start": SEASON_2023_START,
        "assessment_period_end": SEASON_2023_END,
        "annual_ceiling": ceiling,
        "earnings": earnings,
    }
    values.update(overrides)
    return CoidaInput(**values)


# ---------------------------------------------------------------- the golden


@pytest.mark.golden
def test_gn_2390_of_2024_example_1_r600_000_is_declared_at_r568_959():
    """Twelve monthly lines of R50 000 — the cap bites on the YEAR's total, not
    on a month, since no single month comes near it."""
    result = assessment_earnings(an_input(earned(*["50000.00"] * 12)))

    assert result.earnings.rounded == Decimal("600000.00")
    assert result.declared.rounded == Decimal("568959.00")
    assert result.capped is True


@pytest.mark.golden
def test_gn_2390_of_2024_example_2_a_part_year_below_the_cap_is_declared_as_is():
    """ "regardless of whether the said employee worked for a full year or part
    year" — four months of R9 000 is R36 000 declared, and the ceiling is not
    pro-rated to four twelfths to meet it."""
    result = assessment_earnings(an_input(earned(*["9000.00"] * 4)))

    assert result.declared.rounded == Decimal("36000.00")
    assert result.capped is False


@pytest.mark.golden
def test_gn_2390_of_2024_the_full_ceiling_applies_to_a_part_year_employee():
    """ "Full annual maximum earnings will apply irrespective of the number of
    months the employee was employed". Five months at R120 000 — R600 000 — is
    capped at the FULL R568 959, not at five twelfths of it (R237 066)."""
    result = assessment_earnings(an_input(earned(*["120000.00"] * 5)))

    assert result.declared.rounded == Decimal("568959.00")


@pytest.mark.golden
def test_gn_1723_of_2023_the_same_example_on_the_2022_season_ceiling():
    """Same R600 000, the 2022 season's R529 264 — a calculator that took its
    ceiling as an input and still only worked for one year would not have."""
    result = assessment_earnings(
        an_input(
            earned("600000.00"),
            ceiling=CEILING_2022,
            assessment_period_start=datetime.date(2022, 3, 1),
            assessment_period_end=datetime.date(2023, 2, 28),
        )
    )

    assert result.declared.rounded == Decimal("529264.00")


# ------------------------------------------------------------- the boundary


def test_earnings_exactly_at_the_ceiling_are_declared_in_full_and_not_capped():
    """Nothing exceeds the ceiling, so nothing is cut (D-158's standing lesson:
    test the boundary from both sides)."""
    at = assessment_earnings(an_input(earned("568959.00")))
    above = assessment_earnings(an_input(earned("568959.01")))

    assert (at.declared.rounded, at.capped) == (Decimal("568959.00"), False)
    assert (above.declared.rounded, above.capped) == (Decimal("568959.00"), True)


# ---------------------------------------------------------------- the flags


def test_what_counts_is_the_flag_on_the_line_and_nothing_else():
    """The calculator is handed each line's ``is_coida_base`` and reads it. It
    knows no source code: the same 3607 overtime line counts when its flag says
    so and does not when it says not — O-06's question answered by DATA."""
    salary = earned("10000.00")
    overtime_in = (CoidaEarning("3607", Decimal("1500.00"), True),)
    overtime_out = (CoidaEarning("3607", Decimal("1500.00"), False),)

    counted = assessment_earnings(an_input(salary + overtime_in))
    left_out = assessment_earnings(an_input(salary + overtime_out))

    assert counted.declared.rounded == Decimal("11500.00")
    assert left_out.declared.rounded == Decimal("10000.00")
    assert left_out.excluded.rounded == Decimal("1500.00")


def test_an_excluded_line_never_reaches_the_cap_either():
    result = assessment_earnings(
        an_input(earned("500000.00") + earned("200000.00", flagged=False, code="3901"))
    )

    assert result.declared.rounded == Decimal("500000.00")
    assert result.capped is False


def test_the_calculator_carries_no_source_code_list():
    """If somebody adds one — an OVERTIME_CODES, a set of "earnings" codes —
    D-89's single place for the decision has become two."""
    import calculators.coida as module

    source = open(module.__file__, encoding="utf-8").read()
    for code in ("3601", "3605", "3606", "3607", "3901"):
        assert f'"{code}"' not in source, code


# ------------------------------------------------------ reversals and refusals


def test_a_reversal_nets_off_within_the_period():
    result = assessment_earnings(an_input(earned("20000.00", "-5000.00")))
    assert result.declared.rounded == Decimal("15000.00")


def test_a_net_negative_period_declares_nothing_and_says_why():
    result = assessment_earnings(an_input(earned("-5000.00")))

    assert result.declared.rounded == Decimal("0.00")
    assert result.earnings.rounded == Decimal("-5000.00")
    (warning,) = result.trace.warnings
    assert "a reversal outweighs what was paid" in warning


def test_a_period_that_ends_before_it_starts_is_refused():
    with pytest.raises(CoidaInputError, match="ends on 28 February 2023, before it starts"):
        assessment_earnings(
            an_input(earned("1.00"), assessment_period_end=datetime.date(2023, 2, 28))
        )


def test_an_employee_with_no_lines_declares_nil():
    result = assessment_earnings(an_input(()))
    assert (result.declared.rounded, result.capped) == (Decimal("0.00"), False)


# -------------------------------------------------------------------- trace


def test_the_trace_records_every_line_and_the_ceiling_row():
    result = assessment_earnings(an_input(earned("50000.00") + earned("1500.00", code="3607")))

    assert result.trace.inputs["line_001"] == "3601|50000.00|True"
    assert result.trace.inputs["line_002"] == "3607|1500.00|True"
    assert result.trace.inputs["assessment_period_start"] == "2023-03-01"
    assert tuple(result.trace.statutory_rows) == (("statutory_parameter", 2023),)
    assert result.trace.outputs["declared"] == "51500.000000"
