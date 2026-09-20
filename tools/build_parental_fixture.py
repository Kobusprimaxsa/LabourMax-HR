"""Builds the parental leave fixture — Van Wyk's interim reading-in.

Every figure here is transcribed from the Constitutional Court's own copy of the
order in *Van Wyk and Others v Minister of Employment and Labour; Commission for
Gender Equality and Another v Minister of Employment and Labour and Others*
(CCT 308/23) [2025] ZACC 20, handed down 3 October 2025. Nothing is inferred and
nothing comes from commentary on the judgment, of which there is a great deal and
much of it wrong about the two totals.

**The two totals, which are the whole point.** Order para 5(a) reads s25 to give:

    (1) An employee who is-- (a) a single parent; or (b) the only employed party
        in a parental relationship, is entitled to at least four consecutive
        months' parental leave.

    (4A) If both parties to a parental relationship are employed, the parties are
         entitled in the aggregate to four months and ten days' parental leave,
         inclusive of any parental leave taken in terms of subsections (2) and (3).

So four months is the general entitlement and four months and ten days is the
AGGREGATE for two employed parents — not, as commentary often has it, a flat
"four months and ten days" for everybody.

**Both tables LAPSE, in opposite directions** (D-203). Para 4 suspends the
declarations of invalidity for 36 months from the date of the order, so the
reading-in is loaded with an ``effective_to`` of 2 October 2028 and never
open-ended. The quantum simply stops, and the resolver refuses rather than
falling back to the pre-judgment s25A. The adoption age limit is the other way:
para 3 has already declared it invalid and para 4 only delays that, so a second
row from 3 October 2028 says there is no limit.

Run: python tools/build_parental_fixture.py > reference/ref-2026.03.01-parental.json
"""

from __future__ import annotations

import json
import pathlib

ZACC = (
    "Van Wyk v Minister of Employment and Labour (CCT 308/23) [2025] ZACC 20 "
    "(3 October 2025), order para 5(a), reading in BCEA 75 of 1997 s25(1) and s25(4A)"
)
ZACC_ADOPTION = (
    "Van Wyk v Minister of Employment and Labour (CCT 308/23) [2025] ZACC 20 "
    "(3 October 2025), order paras 3, 4 and 5(c), reading in BCEA 75 of 1997 s25B(1)"
)
ZACC_URL = "https://collections.concourt.org.za/handle/20.500.12144/38507"

# Para 4: "suspended for a period of 36 months from the date of this order", and
# the order is dated 3 October 2025. effective_to is EXCLUSIVE everywhere in this
# schema, so the interim rows run to (and include) 2 October 2028.
JUDGMENT_DATE = "2025-10-03"
SUSPENSION_ENDS = "2028-10-03"

INTERIM_NOTE = (
    "INTERIM. The declarations of invalidity are suspended for 36 months from 3 October "
    "2025 (order para 4), so this row is effective-dated to end with the suspension and is "
    "never open-ended. Order para 5 applies the reading-in 'pending the coming into force "
    "of any remedial legislation', which may or may not outlast the suspension - the "
    "conservative reading is loaded, so the figure runs out and a human is asked rather "
    "than the software choosing. Parliament legislating sooner is the likely case: order "
    "para 6 requires the Minister to report by 3 April 2028, and para 7 lets any party "
    "apply for supplementary relief by 3 June 2028."
)

QUANTUM_NOTE = (
    "TWO TOTALS. Read-in s25(1): a single parent, or the only employed party in a parental "
    "relationship, gets at least four consecutive months. Read-in s25(4A): where both "
    "parties are employed, the parties are entitled IN THE AGGREGATE to four months and ten "
    "days, inclusive of leave taken under s25(2) and (3). The ten days are a conditional "
    "increment to an aggregate shared between two people, not an entitlement of their own - "
    "s25A, which gave a standalone ten days, is DELETED by order para 5(b). Which total "
    "applies turns on whether the other parent is employed, which is a fact about somebody "
    "who is not this employer's employee: it is captured as a declaration, never computed "
    "(D-202)."
)

ADOPTION_LIMIT_NOTE = (
    "The limit applies TODAY and is already unconstitutional. Order para 3 declares s25B(1) "
    "invalid to the extent that it limits parental leave to an adopted child below two; para "
    "4 suspends that declaration for 36 months; para 5(c)'s reading-in retains 'below the age "
    "of two' meanwhile. So it binds until the suspension ends and falls away when it does - "
    "see the row that follows this one."
)

ADOPTION_NO_LIMIT_NOTE = (
    "The limit falls away, by operation of the order itself. Para 3's declaration of "
    "invalidity takes effect when para 4's suspension ends, so from this date an adoption is "
    "not limited by the child's age. Loaded as DATA now rather than left to a code change, so "
    "it is right on the day: refusing an adoption of a three-year-old in November 2028 "
    "because 'no rule is loaded' would be this software enforcing a provision the "
    "Constitutional Court has struck down."
)

DOCUMENT = {
    # -2 because the first load of this file went in without the watch dates. The
    # watch rows are NEW DATA, not a re-encoding, so they travel as an ordinary
    # load of a new version rather than through --supersede (D-199's own line).
    "version_label": "REF-2026.03.01-PARENTAL-2",
    "applies_from": "2026-03-01",
    "description": (
        "Parental leave under the interim reading-in in Van Wyk v Minister of Employment and "
        "Labour [2025] ZACC 20. Two totals, and two provisions that lapse in opposite "
        "directions on 3 October 2028."
    ),
    "tables": {
        "parental_leave_quantum": [
            {
                "effective_from": JUDGMENT_DATE,
                "effective_to": SUSPENSION_ENDS,
                "sole_parent_months": 4,
                "sole_parent_days": 0,
                "both_employed_months": 4,
                "both_employed_days": 10,
                "source_reference": ZACC,
                "source_url": ZACC_URL,
                "notes": f"{QUANTUM_NOTE} {INTERIM_NOTE}",
            }
        ],
        "adoption_age_limit": [
            {
                "effective_from": JUDGMENT_DATE,
                "effective_to": SUSPENSION_ENDS,
                "is_limited": True,
                "max_child_age_years": 2,
                "source_reference": ZACC_ADOPTION,
                "source_url": ZACC_URL,
                "notes": f"{ADOPTION_LIMIT_NOTE} {INTERIM_NOTE}",
            },
            {
                "effective_from": SUSPENSION_ENDS,
                "is_limited": False,
                "source_reference": ZACC_ADOPTION,
                "source_url": ZACC_URL,
                "notes": ADOPTION_NO_LIMIT_NOTE,
            },
        ],
        # 5d. Three dates, because the order gives three, and the earliest is the
        # one that tells us whether Parliament is going to act in time.
        "statutory_watch_item": [
            {
                "watch_code": "VAN_WYK_SUSPENSION",
                "name": "Van Wyk: the 36-month suspension ends",
                "description": (
                    "The interim reading-in of BCEA s25, s25B and s25C runs out on 3 October "
                    "2028 (order para 4). parental_leave_quantum is effective-dated to that day "
                    "and NOTHING is loaded after it, so capture REFUSES from then until "
                    "whatever replaces it is loaded (D-203) - it must never fall back to the "
                    "repealed s25A's ten days. Watch for: an amending Act bringing remedial "
                    "legislation into operation, or the suspension simply expiring. On the same "
                    "date the under-two adoption limit FALLS AWAY, and that row is already "
                    "loaded, so nothing needs doing for it."
                ),
                "change_cadence": "as_proclaimed",
                "typical_publication_window": "before 3 October 2028",
                "next_expected_date": "2028-10-03",
                "last_confirmed_date": "2026-09-18",
                "last_change_effective_date": "2025-10-03",
                "source_name": "Constitutional Court of South Africa; Parliament",
                "source_url": ZACC_URL,
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "VAN_WYK_MINISTER_REPORT",
                "name": "Van Wyk: the Minister's report on remedial legislation",
                "description": (
                    "Order para 6: not later than six months before the suspension expires, the "
                    "Minister of Employment and Labour must report to the Registrar on whether "
                    "remedial legislation is in operation and, if not, when it is expected. That "
                    "report is the earliest reliable signal of what replaces the reading-in, and "
                    "of whether to expect a new quantum before 3 October 2028."
                ),
                "change_cadence": "as_proclaimed",
                "typical_publication_window": "by 3 April 2028",
                "next_expected_date": "2028-04-03",
                "last_confirmed_date": "2026-09-18",
                "last_change_effective_date": "2025-10-03",
                "source_name": "Minister of Employment and Labour, via the Registrar",
                "source_url": ZACC_URL,
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "VAN_WYK_SUPPLEMENTARY_RELIEF",
                "name": "Van Wyk: the window for supplementary relief",
                "description": (
                    "Order para 7: any party may apply for supplementary relief to take effect "
                    "when the suspension expires, and must do so not later than four months "
                    "before it. If such an application is brought, the rule that applies from 3 "
                    "October 2028 may be set by a further order rather than by an Act - so watch "
                    "the Court's roll as well as the Government Gazette."
                ),
                "change_cadence": "as_proclaimed",
                "typical_publication_window": "by 3 June 2028",
                "next_expected_date": "2028-06-03",
                "last_confirmed_date": "2026-09-18",
                "last_change_effective_date": "2025-10-03",
                "source_name": "Constitutional Court of South Africa",
                "source_url": ZACC_URL,
                "responsible_role": "Statutory data owner",
            },
        ],
    },
}

# ---------------------------------------------------------------------------
# WRITES the file rather than printing it for redirection (D-264). A shell
# redirect re-encodes: PowerShell's `>` writes the console code page, and
# `python tools/build_notice_band_fixture.py > reference/...json` put U+FFFD
# REPLACEMENT CHARACTER through every non-ASCII character in that file - the
# rand sign and the en dashes in the notes. Nothing refused it: the JSON was
# still valid, the loader read it, and the corruption reached the verification
# workbook as the mojibake a person would have to decide about. Writing with an
# explicit encoding cannot be got wrong from the calling shell.
# ---------------------------------------------------------------------------


OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-parental.json"


def main():
    OUTPUT.write_text(json.dumps(DOCUMENT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
