"""Build ``reference/ref-2026.03.01-termination.json``.

One parameter: BCEA s40(c)'s four-month qualifying period — the service length
below which no pro-rata payment is owed for an INCOMPLETE annual leave cycle on
termination.

Its own ``statutory_parameter`` row, and deliberately NOT reused from
``leave_rule_set.family_resp_min_service_months``, which is also four months:
that one is s27(1)'s family responsibility qualifier and this one is s40(c)'s
termination qualifier. Two statutes, two citations, two figures that happen to
agree today and need not agree tomorrow. Collapsing them would make a change to
one silently move the other.

``test_no_hardcoded_rates`` would not have caught a bare ``4`` here — a month
count is an ``int``, not a ``Decimal`` — which is the same gap D-100 found for
the s43 minimum age and D-220 for the s35(4) averaging window.
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-termination.json"
)

BCEA_URL = "https://www.gov.za/sites/default/files/gcis_document/201409/a75-97.pdf"

FIXTURE = {
    "version_label": "REF-2026.03.01-TERMINATION",
    "applies_from": "2026-03-01",
    "description": (
        "The BCEA s40(c) qualifying period for pro-rata leave on termination. "
        "Researched 19 September 2026 from the Basic Conditions of Employment Act as "
        "published in Government Gazette 18491 of 5 December 1997. NOT verified."
    ),
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "PRO_RATA_LEAVE_MIN_SERVICE_MONTHS",
                "value_numeric": "4.000000",
                "unit": "months",
                "effective_from": "1997-12-01",
                "source_reference": "Basic Conditions of Employment Act 75 of 1997, s40(c)",
                "source_url": BCEA_URL,
                "notes": (
                    "s40: 'On termination of employment, an employer must pay an "
                    "employee ... (c) if the employee has been in employment longer than "
                    "four months, in respect of the employee's annual leave entitlement "
                    "during an incomplete annual leave cycle as defined in section 20(1) "
                    "- (i) one day's remuneration in respect of every 17 days on which "
                    "the employee worked or was entitled to be paid; or (ii) remuneration "
                    "calculated on any basis that is at least as favourable to the "
                    "employee as that calculated in terms of subparagraph (i).' "
                    "LONGER THAN four months, so exactly four months does not qualify - "
                    "the boundary sits with the lower band, read off the words the same "
                    "way D-158 read each notice band's own. "
                    "The 17 is NOT loaded here: it is already "
                    "leave_rule_set.annual_accrual_ratio_days_worked, the same ratio "
                    "s20(2)(b) uses for accrual, and loading a second copy is how the two "
                    "come to disagree. Subparagraph (ii) makes (i) a FLOOR rather than the "
                    "answer, so calculators/termination.py pays the greater of the ledger's "
                    "own accrual and this ratio."
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
