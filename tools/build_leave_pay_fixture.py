"""Build ``reference/ref-2026.03.01-leave-pay.json``.

One parameter: BCEA s35(4)(a)'s averaging window — the "preceding 13 weeks" a
payment under the Act must be calculated by reference to where the employee's
remuneration is on a basis other than time, or fluctuates significantly.

Its own ``statutory_parameter`` row for the same reason
``build_sick_accrual_fixture.py`` gave the six-month sick threshold one, and
``build_employment_fixture.py`` gave the s43 minimum age one (D-100):
``test_no_hardcoded_rates`` would not catch a bare ``13`` here either — it is a
week count, an ``int``, not a ``Decimal`` — and this number decides what an
employee is paid for a week of leave, not a rounding error. The rule is also
statutory rather than sectoral: it sits in the Act, applies to every employer,
and would move only if the Act moved.
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-leave-pay.json"

BCEA_URL = "https://www.gov.za/sites/default/files/gcis_document/201409/a75-97.pdf"

FIXTURE = {
    "version_label": "REF-2026.03.01-LEAVE-PAY",
    "applies_from": "2026-03-01",
    "description": (
        "The BCEA s35(4) averaging window for fluctuating remuneration. Researched "
        "19 September 2026 from the Basic Conditions of Employment Act as published "
        "in Government Gazette 18491 of 5 December 1997. NOT verified."
    ),
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "VARIABLE_EARNINGS_AVERAGE_WEEKS",
                "value_numeric": "13.000000",
                "unit": "weeks",
                "effective_from": "1997-12-01",
                "source_reference": "Basic Conditions of Employment Act 75 of 1997, s35(4)(a)",
                "source_url": BCEA_URL,
                "notes": (
                    "s35(4): 'If an employee's remuneration or wage is calculated, "
                    "either wholly or in part, on a basis other than time or if an "
                    "employee's remuneration or wage fluctuates significantly from "
                    "period to period, any payment to that employee in terms of this "
                    "Act must be calculated by reference to the employee's "
                    "remuneration or wage during (a) the preceding 13 weeks; or (b) "
                    "if the employee has been in employment for a shorter period, "
                    "that period.' The window is the DIVISOR, not the number of weeks "
                    "actually worked - the Act says 'during the preceding 13 weeks' - "
                    "so an unpaid stretch inside the window lowers the average. "
                    "Flagged for the labour law review (O-06). "
                    "WHICH payments make up that remuneration is s35(5) plus the "
                    "Minister's determination in Government Notice 691 of 23 May 2003 "
                    "(Government Gazette 24889, effective 1 July 2003), and in this "
                    "codebase it is the payroll_component.affects_leave_pay_average "
                    "flag - a per-component decision, not a figure, so it is not "
                    "loaded here."
                ),
            },
        ]
    },
}


def main():
    OUTPUT.write_text(json.dumps(FIXTURE, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(OUTPUT.parents[1])}")


if __name__ == "__main__":
    main()
