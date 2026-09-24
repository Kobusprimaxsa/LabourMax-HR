"""calculators/recurring.py — table-driven, every figure worked by hand.

No golden file, permanently (D-150's position): nobody publishes a worked
recurring-deduction example. What is pinned instead is each rule the module
states, and each refusal watched refusing with its message (PROVE EVERY GUARD
FAILS).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.recurring import (
    AccommodationCeiling,
    LineKind,
    RecurringInput,
    RecurringLine,
    RecurringRefusedError,
    recurring_lines,
)

JUNE = datetime.date(2026, 6, 30)
SD7 = AccommodationCeiling(
    capped=True, max_percent=Decimal("10.00"), table="working_time_rule_set", row_id=7
)
BCEA = AccommodationCeiling(capped=False, max_percent=None, table="working_time_rule_set", row_id=1)


def line(n=1, code="LINE", kind=LineKind.DEDUCTION, **fields):
    return RecurringLine(line_id=n, component_code=code, description=code, kind=kind, **fields)


def priced(*lines, basic="5000.00", ceiling=None):
    return recurring_lines(
        RecurringInput(
            calculated_for=JUNE,
            basic=Decimal(basic),
            lines=tuple(lines),
            accommodation_ceiling=ceiling,
        )
    )


def test_a_fixed_earning_and_a_fixed_deduction():
    result = priced(
        line(1, "TRANSPORT", LineKind.EARNING, amount=Decimal("600.00")),
        line(2, "UNION", amount=Decimal("45.50")),
    )
    (earning,) = result.earnings
    (deduction,) = result.deductions
    assert (earning.component_code, earning.amount.rounded) == ("TRANSPORT", Decimal("600.00"))
    assert (deduction.component_code, deduction.amount.rounded) == ("UNION", Decimal("45.50"))
    assert result.trace.outputs == {"TRANSPORT_1": "600.000000", "UNION_2": "45.500000"}
    assert result.trace.statutory_rows == ()


def test_a_percentage_of_basic():
    """10% of R4 321,55 = R432,155 — kept exact, rounded HALF_UP to R432,16."""
    (deduction,) = priced(line(percentage_of_basic=Decimal("10.0000")), basic="4321.55").deductions
    assert deduction.amount.exact == Decimal("432.155000")
    assert deduction.amount.rounded == Decimal("432.16")
    assert deduction.percentage == Decimal("10.0000")
    assert deduction.note == "10.0000% of basic 4321.55"


def test_accommodation_at_the_sd7_ceiling_is_deducted_and_the_row_recorded():
    (deduction,) = priced(
        line(3, "ACCOM_DED", percentage_of_basic=Decimal("10.00"), is_accommodation=True),
        ceiling=SD7,
    ).deductions
    assert deduction.amount.rounded == Decimal("500.00")
    result = priced(
        line(3, "ACCOM_DED", percentage_of_basic=Decimal("10.00"), is_accommodation=True),
        ceiling=SD7,
    )
    assert result.trace.statutory_rows == (("working_time_rule_set", 7),)


def test_accommodation_above_the_sd7_ceiling_refuses():
    with pytest.raises(RecurringRefusedError, match=r"R?550\.00 is more than the 10\.00%"):
        priced(
            line(3, "ACCOM_DED", percentage_of_basic=Decimal("11.00"), is_accommodation=True),
            ceiling=SD7,
        )


def test_accommodation_under_an_instrument_stating_no_ceiling_is_not_capped():
    """The BCEA states no accommodation percentage: the ABSENCE of a cap (D-198)."""
    (deduction,) = priced(
        line(percentage_of_basic=Decimal("25.00"), is_accommodation=True), ceiling=BCEA
    ).deductions
    assert deduction.amount.rounded == Decimal("1250.00")


def test_accommodation_with_no_rule_set_handed_in_refuses():
    with pytest.raises(RecurringRefusedError, match="accommodation ceiling cannot be checked"):
        priced(line(percentage_of_basic=Decimal("5.00"), is_accommodation=True))


def test_the_lines_own_ceiling_clamps_and_says_so():
    """R1 000 on a line capped at 10% of R5 000 → R500, with a note and a warning."""
    result = priced(line(4, "ADVANCE_DED", amount=Decimal("1000.00"), cap_percent=Decimal("10")))
    (deduction,) = result.deductions
    assert deduction.amount.rounded == Decimal("500.00")
    assert deduction.note == "held to the line's own 10% ceiling"
    assert result.trace.warnings == (
        "ADVANCE_DED_4: 1000.00 held to 500.00, the line's own 10% of basic ceiling.",
    )


def test_a_ceiling_that_is_not_reached_changes_nothing():
    (deduction,) = priced(line(amount=Decimal("100.00"), cap_percent=Decimal("10"))).deductions
    assert (deduction.amount.rounded, deduction.note) == (Decimal("100.00"), "")


def test_a_ceiling_on_an_earning_is_not_applied():
    """A ceiling limits what is TAKEN; it has no meaning on an earning."""
    (earning,) = priced(
        line(kind=LineKind.EARNING, amount=Decimal("900.00"), cap_percent=Decimal("10"))
    ).earnings
    assert earning.amount.rounded == Decimal("900.00")


def test_a_loans_last_instalment_is_what_is_owed():
    (deduction,) = priced(
        line(5, "ADVANCE_DED", amount=Decimal("400.00"), owed=Decimal("200.00"))
    ).deductions
    assert deduction.amount.rounded == Decimal("200.00")
    assert deduction.note == "final instalment: 200.00 was owed"


def test_a_loan_with_more_owed_than_the_instalment_takes_the_instalment():
    (deduction,) = priced(line(amount=Decimal("400.00"), owed=Decimal("1000.00"))).deductions
    assert (deduction.amount.rounded, deduction.note) == (Decimal("400.00"), "")


def test_a_loan_paid_off_produces_no_line():
    result = priced(line(6, "ADVANCE_DED", amount=Decimal("400.00"), owed=Decimal("0.00")))
    assert result.deductions == ()
    assert result.trace.outputs == {"ADVANCE_DED_6": "nothing owed"}


def test_a_zero_figure_produces_no_line():
    """A percentage of a nil basic — an hourly worker with no ordinary hours."""
    result = priced(line(7, "X", percentage_of_basic=Decimal("10")), basic="0")
    assert result.deductions == ()
    assert result.trace.outputs == {"X_7": "0.000000"}
