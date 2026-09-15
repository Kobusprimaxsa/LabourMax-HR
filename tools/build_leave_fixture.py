"""Build ``reference/ref-2026.03.01-leave.json``.

One parameter: BCEA s23(1)'s certificate threshold. Placed in its own
``statutory_parameter`` fixture rather than as a literal on
``leave_evidence_type`` (P6 chunk 2's own catalogue seed), for the same
reason ``build_employment_fixture.py`` gave the minimum employment age its
own row rather than a literal in ``employees/engagements.py``:
``test_no_hardcoded_rates`` would not catch this one either — it is an
``int`` (a day count), not a ``Decimal`` — and the number decides whether an
employer may lawfully withhold pay, which is not a rounding error.
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-leave.json"

FIXTURE = {
    "version_label": "REF-2026.03.01-LEAVE",
    "applies_from": "2026-03-01",
    "description": (
        "Leave evidence thresholds not carried by leave_rule_set. Researched 15 "
        "September 2026 from the Basic Conditions of Employment Act. NOT verified."
    ),
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS",
                "value_numeric": "2.000000",
                "unit": "days",
                "effective_from": "1997-12-01",
                "source_reference": (
                    "Basic Conditions of Employment Act 75 of 1997, s23(1). "
                    "Commencement of Chapter 6."
                ),
                "source_url": "https://www.labour.gov.za/DocumentCenter/Acts/"
                "Basic%20Conditions%20of%20Employment/Act%20-%20Basic%20Conditions"
                "%20of%20Employment.pdf",
                "notes": (
                    "s23(1): an employer is not required to pay an employee in "
                    "accordance with s22 (sick leave) if the employee has been absent "
                    "from work for more than TWO consecutive days, or on more than two "
                    "occasions during an eight-week period, and, on request, does not "
                    "produce a medical certificate. The Act does NOT make a certificate "
                    "a condition of taking the leave itself — it conditions PAY. This "
                    "figure is the consecutive-day limb only; the two-occasions-in-"
                    "eight-weeks limb is not yet modelled anywhere in this schema and "
                    "leave/evidence.py's own module docstring flags that gap rather "
                    "than silently only checking the day count."
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
