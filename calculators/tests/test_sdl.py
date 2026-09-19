"""SDL — Skills Development Levies Act 9 of 1999.

**There is no published worked example to be golden against, and this says so
rather than inventing one** (D-210, following D-150's precedent). SARS's SDL
employer guide (SDL-GEN-01-G01) gives the four steps and the rate; its 2026 PAYE
employer guide gives the rate; neither works a number. A figure fabricated from
this codebase's own arithmetic and labelled golden would only ever agree with
itself. What IS published and is tested here is the rate — "From 1 April 2001,
at a rate of 1 per cent of the leviable amount" (s3(1)(a)(ii), repeated in
SDL-GEN-01-G01 §6) — and the exemption's own words.

The statutory words each case tests:

- s3(1)(a)(ii): one per cent of the leviable amount
- s3(3): the leviable amount is the total remuneration as determined under the
  Fourth Schedule
- s4(b): the levy is not payable where "there are reasonable grounds for
  believing that the total amount of remuneration ... paid or payable by that
  employer to all its employees during the FOLLOWING 12 month period will not
  exceed R500 000" — forward-looking, so not computable from history
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.sdl import SdlInput, levy

MARCH = datetime.date(2026, 3, 31)

RATE = StatutoryFigure(
    value=Decimal("1.000000"),
    table="statutory_parameter",
    row_id=801,
    description="SDL_RATE_PCT, SDL Act s3(1)(a)(ii)",
)


def an_input(**overrides) -> SdlInput:
    values = {
        "calculated_for": MARCH,
        "leviable_amount": Decimal("100000.00"),
        "rate_percent": RATE,
        "employer_is_liable": True,
    }
    values.update(overrides)
    return SdlInput(**values)


# --------------------------------------------------------------- the rate


@pytest.mark.golden
def test_the_published_rate_is_one_per_cent_of_the_leviable_amount():
    """s3(1)(a)(ii), and SDL-GEN-01-G01 §6: "From 1 April 2001, at a rate of 1
    per cent of the leviable amount"."""
    result = levy(an_input(leviable_amount=Decimal("100000.00")))
    assert result.levy.rounded == Decimal("1000.00")


def test_the_levy_follows_the_leviable_amount():
    assert levy(an_input(leviable_amount=Decimal("45678.90"))).levy.rounded == Decimal("456.79")
    assert levy(an_input(leviable_amount=Decimal("0.00"))).levy.rounded == Decimal("0.00")


def test_the_unrounded_figure_travels_with_the_rounded_one():
    result = levy(an_input(leviable_amount=Decimal("45678.90")))
    assert result.levy.exact == Decimal("456.789000")
    assert result.levy.rounded == Decimal("456.79")


# ------------------------------------------------------- the exemption flag


def test_an_exempt_employer_pays_nothing():
    result = levy(an_input(employer_is_liable=False))
    assert result.levy.rounded == Decimal("0.00")
    assert result.trace.outputs["employer_is_liable"] == "False"


def test_the_calculator_will_not_compute_the_sdl_exemption_and_cannot_be_given_a_payroll_history():
    """s4(b) asks whether there are reasonable grounds for believing the total
    remuneration over the FOLLOWING twelve months will not exceed R500 000.

    That is a forward-looking belief about a future the employer alone can
    assess — a new employer with two staff and a hiring plan is liable; one
    winding down is not, on identical history. It CANNOT be computed from what
    the payroll has paid so far, and a calculator that tried would register an
    employer who should not be registered, or fail to register one who should.

    So liability arrives as a declared boolean and there is nowhere to hand this
    function a history, a threshold or a twelve-month total. If somebody adds
    one, this test fails and they have to read s4(b) before going further.
    """
    fields = set(SdlInput.__dataclass_fields__)

    assert "employer_is_liable" in fields
    forbidden = {
        name
        for name in fields
        if any(word in name for word in ("history", "annual", "threshold", "previous", "last_12"))
    }
    assert not forbidden, (
        f"SdlInput takes {sorted(forbidden)}. The s4(b) exemption is a FORWARD-LOOKING belief "
        "about the next twelve months, which no history can establish: it is a human-set flag "
        "(D-209). The R500 000 threshold is an input to the SCREEN that asks the human, never "
        "to this calculator."
    )
    with pytest.raises(TypeError):
        SdlInput(
            calculated_for=MARCH,
            leviable_amount=Decimal("100000.00"),
            rate_percent=RATE,
            employer_is_liable=True,
            previous_twelve_month_payroll=Decimal("450000.00"),
        )


# ------------------------------------------------------------------ the trace


def test_the_result_carries_the_row_it_was_computed_from():
    result = levy(an_input())
    assert result.trace.calculator == "sdl.levy"
    assert result.trace.calculated_for == MARCH
    assert ("statutory_parameter", 801) in result.trace.statutory_rows


def test_an_exempt_employer_still_produces_a_trace():
    """A missing trace row must mean "this never ran" and nothing else (D-208)."""
    result = levy(an_input(employer_is_liable=False))
    assert result.trace.statutory_rows
    assert result.trace.outputs["levy"] == "0.000000"


def test_a_negative_leviable_amount_is_treated_as_nil_and_warns():
    """A correction belongs in the month it corrects, not as a negative levy."""
    result = levy(an_input(leviable_amount=Decimal("-500.00")))

    assert result.levy.rounded == Decimal("0.00")
    assert result.leviable_amount.exact == Decimal("0.000000")
    assert any("negative" in w for w in result.trace.warnings), result.trace.warnings


def test_an_exemption_with_no_reason_recorded_warns():
    result = levy(an_input(employer_is_liable=False, exemption_reason=""))
    assert any("no reason" in w for w in result.trace.warnings), result.trace.warnings


def test_an_exemption_with_a_reason_does_not_warn():
    result = levy(
        an_input(employer_is_liable=False, exemption_reason="s4(b): payroll under R500 000")
    )
    assert result.trace.warnings == ()
    assert "R500 000" in result.trace.outputs["exemption_reason"]
