"""Build ``reference/ref-2026.03.01-employment.json``.

One parameter: the age below which a person may not be employed at all. Here
rather than as a literal ``15`` in ``employees/`` because CLAUDE.md's rule covers
thresholds, not only rates — and because the automated guard would NOT have caught
it: ``test_no_hardcoded_rates`` scans for ``Decimal`` and ``float`` literals, and an
age is an ``int``. Getting this wrong in the permissive direction is not a payslip a
few rand out; it is a criminal offence under s43(3), committed by the employer, with
this system having recorded the engagement.

The rate-derivation constant from s35 lives in ``build_remuneration_fixture.py``,
in its own version. It started out in this file and the loader refused the second
load, exactly as designed — a version already loaded is never edited in place. The
refusal produced the better structure: employment eligibility and rate derivation
are different subjects with different reasons to change.
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-employment.json"
)

FIXTURE = {
    "version_label": "REF-2026.03.01-EMPLOYMENT",
    "applies_from": "2026-03-01",
    "description": (
        "Employment eligibility thresholds. Researched 13 September 2026 from the "
        "Basic Conditions of Employment Act. NOT verified."
    ),
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "MINIMUM_EMPLOYMENT_AGE",
                "value_numeric": "15.000000",
                "unit": "years",
                "effective_from": "1997-12-01",
                "source_reference": (
                    "Basic Conditions of Employment Act 75 of 1997, s43(1). "
                    "Commencement of Chapter 6."
                ),
                "source_url": "https://www.labour.gov.za/DocumentCenter/Acts/"
                "Basic%20Conditions%20of%20Employment/Act%20-%20Basic%20Conditions"
                "%20of%20Employment.pdf",
                "notes": (
                    "s43(1): a person must not require or permit a child to work if the "
                    "child is under 15 years of age, OR is under the minimum school-leaving "
                    "age in terms of any law. TWO limbs, and only the first is stored here. "
                    "The second is currently also 15 - South African Schools Act 84 of 1996 "
                    "s3(1) requires attendance until the last school day of the year in "
                    "which the learner turns 15, or the ninth grade, whichever comes first - "
                    "so the two coincide today and this single figure is the operative "
                    "floor. IF THE SCHOOL-LEAVING AGE MOVES, THIS ROW MUST BE SUPERSEDED, "
                    "because the Act takes the HIGHER of the two and nothing in the schema "
                    "would notice on its own. "
                    "s43(2) separately forbids work that is inappropriate for the child's "
                    "age or that risks their well-being, education, health or development, "
                    "and s43(3) makes a contravention of either subsection an offence. "
                    "Neither of those is a number and neither is encoded: they are a "
                    "judgement about the work, which this system cannot make. "
                    "effective_from is the commencement of the Act rather than 1 March "
                    "2026, because the threshold has applied continuously since then and "
                    "an engagement backdated to 2015 must resolve against it too."
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
