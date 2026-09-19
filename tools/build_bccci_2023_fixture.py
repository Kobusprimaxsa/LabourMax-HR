"""Build ``reference/ref-2023.04.01-bccci.json`` — the PREDECESSOR BCCCI agreement.

The Bargaining Council for the Contract Cleaning Services Industry
(KwaZulu-Natal) Main Collective Agreement extended to non-parties by NOTICE 1726
OF 2023 in Government Gazette 48356, 31 March 2023, signed by Minister T W Nxesi.

**This is the instrument that governed 1–31 March 2026 and that O-30 was open
for.** The 2026 agreement's clause 2(1)(a) says "the parties agree that the
current Main Agreement shall continue to be enforced" and its clause 2(3)
carries prevailing terms forward until replacement; this agreement's own
extension notice binds non-parties "with effect from the first day of the month
after the date of publication of this Notice and shall remain in force until
replaced by a subsequent agreement". So its last rate ran until the 2026
agreement took effect on 1 April 2026, and before this was loaded a KwaZulu-Natal
contract cleaning payroll for March 2026 refused by name (D-238).

**Found, reported, then loaded — three passes, deliberately.** The gazette was
located and rendered on 19 September 2026, its figures transcribed and reported
on 20 September, and only then loaded. Finding and loading in one pass is what
produced the corrected citations the first time round.

**The body text is not extractable** — the PDF has no usable font encoding, so
the pages were rendered and read. Clause 4.1(a) and clause 2(1) were read off
page 39 and page 37 of the gazette respectively, and clause 3's "monthly wage"
off page 38.

**The three rates close each other and the last closes on the successor.** The
2025 rate runs to 1 April 2026 exclusive, which is the day the 2026 agreement's
own first rate begins, so the two agreements abut with neither a gap nor an
overlap for ``minimum_wage_rate``'s exclusion constraint to catch.

**What this does NOT load.** Only the wage rates and the monthly wage factor
were read and verified. This agreement's leave, working time, termination and
notice provisions are not loaded, so for 1–31 March 2026 those still resolve to
Sectoral Determination 1's sector-wide rows, which is what they did before and
is an approximation rather than the instrument. Recorded rather than papered
over (D-259).

Run: python tools/build_bccci_2023_fixture.py
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2023.04.01-bccci.json"

#: Same shape as the 2026 agreement's citation (D-257): the council's full name,
#: the province abbreviated, and the notice and gazette that carry it. No
#: identifier is shared with the 2026 one, so checkstatutory's near-duplicate
#: check reads them as the two different documents they are.
GAZETTE = (
    "Bargaining Council for the Contract Cleaning Services Industry (KZN) "
    "Main Collective Agreement, Notice 1726 of 2023 in GG 48356, 31 March 2023"
)
GAZETTE_URL = "https://www.gov.za/sites/default/files/gcis_document/202304/48356gen1726.pdf"

DERIVED_START = (
    "DERIVED DATE, not printed. Clause 4.1(a)(i) gives this rate 'with effect from the "
    "period of operation', clause 2(1) puts the period of operation at 'the 1st day of "
    "the month following the date of promulgation', and the extension notice binds "
    "non-parties 'with effect from the first day of the month after the date of "
    "publication of this Notice'. Published 31 March 2023, therefore 1 April 2023. "
    "Check the two clauses rather than trusting this note."
)

CLOSED_BY_SUCCESSOR = (
    "CLOSED BY ITS SUCCESSOR, not by this agreement. Nothing here states an end date: "
    "the extension notice says the agreement 'shall remain in force until replaced by a "
    "subsequent agreement' and clause 2(3) carries prevailing terms forward until a new "
    "one is promulgated. So this rate governed until the 2026 agreement (GN R.7296 in "
    "GG 54412) took effect on 1 April 2026, and that is where effective_to comes from. "
    "IT IS THE RATE THAT APPLIED IN MARCH 2026, which is the month a KwaZulu-Natal "
    "payroll used to refuse on (O-30)."
)

AREA_B = (
    "Area B is all of KwaZulu-Natal. Sectoral Determination 1 states no figure for it "
    "and points at this council's agreement; sector_area.uses_bargaining_council_rates "
    "records that, and statutory/resolve.py refuses rather than falling back to the "
    "National Minimum Wage where no row is loaded."
)

NOT_A_GAZETTEER = (
    "Only the wage rates and the clause 3 monthly wage factor were read and verified "
    "from this gazette. Its leave, working time, termination and notice provisions are "
    "NOT loaded, so for 1-31 March 2026 those resolve to Sectoral Determination 1's "
    "sector-wide rows - the same approximation as before, not this instrument (D-259)."
)


def wage(rate: str, effective_from: str, effective_to: str, subclause: str, extra: str) -> dict:
    return {
        "sector": "CONTRACT_CLEANING",
        "sector_area": "AREA_B",
        "hours_band": "all",
        "hourly_rate": rate,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "source_reference": f"{GAZETTE}, clause 4.1(a)({subclause})",
        "source_url": GAZETTE_URL,
        "notes": " ".join(
            [
                "Clause 4.1(a): per hour or part thereof, calculated on a pro rata basis "
                "for all employees, for the province of Kwa-Zulu Natal.",
                AREA_B,
                extra,
                NOT_A_GAZETTEER,
            ]
        ),
    }


FIXTURE = {
    "version_label": "REF-2023.04.01-BCCCI",
    "applies_from": "2023-04-01",
    "description": (
        "The PREDECESSOR BCCCI (KwaZulu-Natal) Main Collective Agreement: wage rates and "
        "the monthly wage factor, from Notice 1726 of 2023 in GG 48356, 31 March 2023. "
        "This is the instrument that governed 1-31 March 2026 (O-30). Read off the "
        "rendered gazette; the PDF's body text does not extract. NOT verified."
    ),
    "tables": {
        "minimum_wage_rate": [
            wage("27.5000", "2023-04-01", "2024-03-01", "i", DERIVED_START),
            wage("29.1200", "2024-03-01", "2025-03-01", "ii", ""),
            wage("30.8600", "2025-03-01", "2026-04-01", "iii", CLOSED_BY_SUCCESSOR),
        ],
        "statutory_parameter": [
            {
                "parameter_code": "MONTHLY_TO_WEEKLY_FACTOR",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "4.330000",
                "unit": "ratio",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 3, definition of 'monthly wage'",
                "source_url": GAZETTE_URL,
                "notes": (
                    "Clause 3: \"'monthly wage' shall mean the hours normally worked in a "
                    "week multiplied by the rate applicable as stipulated in clause 4 and "
                    'multiplied by 4.33." Word for word what the 2026 agreement says, so '
                    "the factor did not move when the agreement was replaced - but it is "
                    "loaded from this instrument for this period rather than reached "
                    "backwards from the next one. Without it a March 2026 monthly wage "
                    "would fall back to the unscoped BCEA s35(3) figure of 4.333333, "
                    "which is a different number and not the one that bound these "
                    "employees (D-236). Closed at 1 April 2026, where the successor's own "
                    "scoped row begins."
                ),
            }
        ],
    },
}


def main():
    OUTPUT.write_text(json.dumps(FIXTURE, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(OUTPUT.parents[1])}")


if __name__ == "__main__":
    main()
