"""Build ``reference/ref-2026.04.01-bccci.json`` — the BCCCI Main Agreement.

The Bargaining Council for the Contract Cleaning Services Industry
(KwaZulu-Natal) Main Collective Agreement, extended to non-parties by GN R.7296
in Government Gazette 54412, 27 March 2026 (Regulation Gazette 11965, Vol. 729),
signed by the Minister on 18 March 2026, agreement signed at Durban on
28 January 2026.

**This is the instrument SD1 points at for Area B and that nobody had.** The
gazetted sectoral determination states no figure for KwaZulu-Natal at all — it
names this agreement instead (D-118) — so until now a KwaZulu-Natal contract
cleaning employer could not be onboarded.

**Its own version, not an extension of REF-2026.03.01, for three reasons.** It
applies from a different date (1 April 2026, not 1 March); it comes from a
different instrument with its own promulgation and its own citation; and the
loader would refuse to fold new rows into an existing label anyway — a
``--supersede`` load permits a re-encoding of rows that already exist and
refuses a file carrying rows the database does not have (D-199). A new gazette
gets a new version. That is the rule this fixture is an instance of.

**The 1 April 2026 date is DERIVED and the derivation is on the row.** Clause
4.1(a)(i) gives the first rate "with effect from the 1st day of the month
following the date of promulgation by the Minister", and the extension notice
says the agreement binds non-parties "with effect from the first day of the
month after the date of publication of this Notice". Published 27 March 2026,
so 1 April 2026. Nothing in the gazette prints that date; it is read off two
clauses, and the note says so, so the next person can check it rather than
trust it.

**Two of the three rates take effect after today**, which is new for this
system: 1 March 2027 and 1 March 2028 are loaded now and resolve when their
dates arrive. ``effective_to`` is left open on each and the next row's
``effective_from`` closes it under the half-open convention.

**The monthly factor is the other figure here, and it is not 4,333.** Clause 3
defines "monthly wage" as the hours normally worked in a week times the clause 4
rate times **4.33**, where BCEA s35(4) makes monthly remuneration four and
one-third times weekly. Both bind; which one applies is a fact about the
instrument governing the employee, so the factor is loaded scoped to contract
cleaning Area B and the BCEA figure remains the unscoped fallback (D-236).
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.04.01-bccci.json"

GAZETTE = (
    "Bargaining Council for the Contract Cleaning Services Industry (KwaZulu-Natal) "
    "Main Collective Agreement, GN R.7296 in Government Gazette 54412, 27 March 2026"
)
GAZETTE_URL = "https://www.gov.za/sites/default/files/gcis_document/202603/54412rg11965gon7296.pdf"

#: Why the first rate starts on 1 April 2026 when no date is printed.
DERIVED_START = (
    "DERIVED DATE, not printed. Clause 4.1(a)(i) gives this rate 'with effect from the "
    "1st day of the month following the date of promulgation by the Minister', and the "
    "extension notice binds non-parties 'with effect from the first day of the month "
    "after the date of publication of this Notice'. Published 27 March 2026, therefore "
    "1 April 2026. Check the two clauses rather than trusting this note."
)

AREA_B = (
    "Area B is all of KwaZulu-Natal. Sectoral Determination 1 states no figure for it "
    "and points at this agreement; sector_area.uses_bargaining_council_rates records "
    "that, and statutory/resolve.py refuses rather than falling back to the National "
    "Minimum Wage where no row is loaded."
)

MARCH_GAP = (
    "NOTHING IS LOADED FOR 1-31 MARCH 2026. That month is not a vacuum: clause 2(1)(a) "
    "says 'the parties agree that the current Main Agreement shall continue to be "
    "enforced' and clause 2(3) carries prevailing terms forward until replacement, so a "
    "PREDECESSOR BCCCI agreement governs it. That predecessor is not loaded and is not "
    "held by this project."
)


def wage(
    rate: str,
    effective_from: str,
    subclause: str,
    *,
    effective_to: str | None = None,
    extra: str = "",
) -> dict:
    notes = [
        "Clause 4.1(a): per hour or part thereof, calculated on a pro rata basis for "
        "all employees, for the province of Kwa-Zulu Natal.",
        AREA_B,
    ]
    if extra:
        notes.append(extra)
    row = {
        "sector": "CONTRACT_CLEANING",
        "sector_area": "AREA_B",
        "hours_band": "all",
        "hourly_rate": rate,
        "effective_from": effective_from,
        "source_reference": f"{GAZETTE}, clause 4.1(a)({subclause})",
        "source_url": GAZETTE_URL,
        "notes": " ".join(notes),
    }
    # Each rate CLOSES its predecessor explicitly. effective_to is EXCLUSIVE, so
    # the successor's own start date is the right value: the 2026 rate runs up
    # to but not including 1 March 2027. Three open-ended rows would overlap,
    # and minimum_wage_rate's exclusion constraint refuses them — which is how
    # this was caught rather than shipped as three rates all in force at once.
    if effective_to is not None:
        row["effective_to"] = effective_to
    return row


FIXTURE = {
    "version_label": "REF-2026.04.01-BCCCI",
    "applies_from": "2026-04-01",
    "description": (
        "BCCCI (KwaZulu-Natal) Main Collective Agreement wage rates and monthly wage "
        "factor. Transcribed from GN R.7296 in GG 54412, 27 March 2026, page by page "
        "against the gazette. NOT verified."
    ),
    "tables": {
        "minimum_wage_rate": [
            wage(
                "32.4000",
                "2026-04-01",
                "i",
                effective_to="2027-03-01",
                extra=f"{DERIVED_START} {MARCH_GAP}",
            ),
            wage("34.0200", "2027-03-01", "ii", effective_to="2028-03-01"),
            wage("35.7200", "2028-03-01", "iii"),
        ],
        "statutory_parameter": [
            {
                "parameter_code": "MONTHLY_TO_WEEKLY_FACTOR",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "4.330000",
                "unit": "ratio",
                "effective_from": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 3, definition of 'monthly wage'",
                "source_url": GAZETTE_URL,
                "notes": (
                    "Clause 3: \"'monthly wage' shall mean the hours normally worked in a "
                    "week multiplied by the rate applicable as stipulated in clause 4 and "
                    'multiplied by 4.33." This is NOT the BCEA s35(4) figure. BCEA s35(3) '
                    "makes monthly remuneration four and ONE-THIRD times weekly, loaded "
                    "unscoped as 4.333333; this agreement makes it 4.33 for the employees "
                    "it covers. Two binding figures for one concept, differing by the "
                    "instrument that governs the employee, which is why the parameter is "
                    "scoped rather than replaced (D-236). Do not confuse either with "
                    "termination_rule_set.annual_bonus_weeks = 4.333, which is SD1's "
                    "December bonus QUANTITY and a different figure that happens to look "
                    "almost identical."
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
