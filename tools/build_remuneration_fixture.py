"""Build ``reference/ref-2026.03.01-remuneration.json``.

One parameter, and it is the only statutory constant in rate derivation.

BCEA s35 sets an employee's monthly remuneration at **four and one-third times**
the weekly wage. Every conversion between pay bases passes through it — monthly to
weekly, weekly to monthly, and both of those onward to hourly and daily — so a
monthly salary cannot become an hourly rate without it.

Written as ``Decimal("4.333333")`` inside ``employees/remuneration.py`` it would
have been a gazetted figure hiding in arithmetic. That one ``test_no_hardcoded_rates``
*would* have caught, which is the difference between this row and the minimum
employment age in the sibling fixture: an age is an ``int`` and slips past the guard,
a factor is a ``Decimal`` and does not. Both belong in a table for the same reason;
only one of them had a test watching.
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-remuneration.json"
)

FIXTURE = {
    "version_label": "REF-2026.03.01-REMUNERATION",
    "applies_from": "2026-03-01",
    "description": (
        "Rate derivation constants from the Basic Conditions of Employment Act. "
        "Researched 13 September 2026. NOT verified."
    ),
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "MONTHLY_TO_WEEKLY_FACTOR",
                "value_numeric": "4.333333",
                "unit": "ratio",
                "effective_from": "1997-12-01",
                "source_reference": (
                    "Basic Conditions of Employment Act 75 of 1997, s35, read with "
                    "Form BCEA1A (Regulation 2), Summary of the Act"
                ),
                "source_url": "https://www.wits.ac.za/media/wits-university/about-wits/"
                "documents/Form%20BCEA1A%20-%20Summary%20of%20the%20Act%20-%20English.pdf",
                "notes": (
                    "Form BCEA1A, the Department of Employment and Labour's own "
                    "summary that every employer must display, states it as: 'Monthly "
                    "remuneration or wage is four and one-third times the weekly "
                    "wage.' "
                    "THE ACT SAYS 'FOUR AND ONE-THIRD', WHICH IS EXACTLY 13/3. The "
                    "stored value is that rounded to this column's six decimal places, "
                    "so 4.333333 rather than 4.333333... . The error is under one part "
                    "in ten million and sits well inside the four-to-six decimal "
                    "working precision invariant 6 requires of intermediate values; "
                    "rounding to the payslip line happens once, at two decimals, long "
                    "after this. "
                    "It is NOT 52/12 by coincidence. 52/12 is the same number, but the "
                    "citation is s35 rather than the calendar, and an amendment would "
                    "move this figure without moving the number of weeks in a year. "
                    "Sectoral Determination 1 uses '4,333 weeks' for the contract "
                    "cleaning December bonus - the same factor at three decimals as "
                    "that gazette published it. That is a separate gazetted figure in "
                    "its own rule set row and must not be replaced by this one."
                ),
            }
        ]
    },
}


def main():
    OUTPUT.write_text(json.dumps(FIXTURE, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(OUTPUT.parents[1])}")


if __name__ == "__main__":
    main()
