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

# O-33: one instrument, one citation. This agreement was cited two ways -
# the council's full name with 'Government Gazette' spelled out on the wage
# and rule set rows, and a short 'BCCCI ... GG' form on the notice bands,
# where the long one plus a pinpoint would not fit source_reference's 200
# characters. The verification workbook groups by source document, so the
# two spellings read as two documents and a person could verify one to
# completion with the version still showing incomplete (D-257). This form
# keeps the council's full name, abbreviates only the province, and leaves
# room for the longest pinpoint in the set.
GAZETTE = (
    "Bargaining Council for the Contract Cleaning Services Industry (KZN) "
    "Main Collective Agreement, GN R.7296 in GG 54412, 27 March 2026"
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
    "version_label": "REF-2026.04.01-BCCCI-r2",
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


# ---------------------------------------------------------------------------
# THE TWO ADDITIVE MATERNITY BENEFITS, clause 13.2 and clause 13.4(a).
#
# Most of clause 13 is NOT loaded. Clause 13.3 compels an employee to cease
# work eight weeks before confinement and caps her return at twelve weeks after
# the birth, where an employee who works to the birth and then takes her four
# consecutive months under the read-in BCEA s25(1) is entitled to roughly
# seventeen and a half weeks after it. Twelve is less, and BCEA s49(1)(d)
# forbids a bargaining council agreement from reducing the s25 entitlement, so
# 13.3 and 17(b) are VOID to that extent and are recorded in the register
# rather than loaded as rules.
#
# These two are different in kind: they ADD to what the Act gives and take
# nothing from anyone, so s49 does not reach them and they are separable from
# the void parts of the same clause (D-244).
#
# CLAUSE 13.4(a) IS STORED AS A DIVISOR, NOT AS A THIRD. "One third of one
# month's wage" as 0.333333 is a rounded figure standing where an exact one
# belongs, and it is money: a third of R9 000 is R3 000 exactly, while
# 0.333333 x 9000 is R2 999,997. The clause says a third, so the DIVISOR is
# what is loaded and the calculator divides.
#
# CLAUSE 13.5 extends clause 13 to still births and to legal adoptions of a
# child under one year old. HOW FAR THAT REACHES THESE TWO IS A READING, and
# it is recorded as one rather than asserted (O-06):
#
#   * A STILL BIRTH reaches BOTH. The pregnancy ran, the clinic visits in
#     13.2 were attended before anyone knew the outcome, and 13.4(a) attaches
#     to the return from leave rather than to a living child. Withholding
#     either on the outcome would read a condition into 13.5 that the clause
#     does not contain, and 13.5 extends "clause 13", not part of it.
#   * AN ADOPTION reaches 13.4(a) and CANNOT reach 13.2. 13.2 pays for
#     attendance at a prenatal clinic in each of the three months before the
#     expected date of confinement; an adoptive parent has no confinement for
#     those months to precede. That is the benefit being incapable of
#     application, not the agreement excluding it - so nothing is loaded that
#     says an adoptive parent is refused it, and whatever prices 13.2 will
#     find no clinic attendance to pay for.
#
# Neither reading changes a figure. Both change who a figure reaches, which is
# why they are flagged for the labour law review rather than settled here.
# ---------------------------------------------------------------------------

MATERNITY_OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "reference"
    / "ref-2026.04.01-bccci-maternity.json"
)

CLAUSE_13_5 = (
    "Clause 13.5 extends clause 13 to a still birth and to a legal adoption of a child "
    "under one year old. READ AS: a still birth reaches this benefit; an adoption "
    "reaches clause 13.4(a) and cannot reach clause 13.2, which pays for prenatal "
    "clinic attendance an adoptive parent has no confinement to precede. A reading, "
    "flagged for the labour law review, not a figure (O-06)."
)

ADDITIVE = (
    "ADDITIVE, which is why it is loaded when most of clause 13 is not. BCEA s49(1)(d) "
    "forbids a bargaining council agreement from REDUCING the s25 entitlement; clause "
    "13.3's twelve-week cap does that and is void to that extent. This benefit takes "
    "nothing from anyone and is separable from it (D-244)."
)

MATERNITY_FIXTURE = {
    "version_label": "REF-2026.04.01-BCCCI-MATERNITY-r2",
    "applies_from": "2026-04-01",
    "description": (
        "The two ADDITIVE maternity benefits in the BCCCI Main Agreement, clause 13.2 "
        "and clause 13.4(a), scoped to contract cleaning Area B. The rest of clause 13 "
        "is void under BCEA s49(1)(d) and is not loaded. NOT verified."
    ),
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "PRENATAL_CLINIC_PAID_DAYS_PER_MONTH",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "1.000000",
                "unit": "days",
                "effective_from": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 13.2",
                "source_url": GAZETTE_URL,
                "notes": (
                    "One day's FULLY PAID leave, on satisfactory proof of attendance at a "
                    "prenatal clinic. Paid leave, not time off: the day costs the employee "
                    f"nothing. {ADDITIVE} {CLAUSE_13_5}"
                ),
            },
            {
                "parameter_code": "PRENATAL_CLINIC_MONTHS_BEFORE_BIRTH",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "3.000000",
                "unit": "months",
                "effective_from": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 13.2",
                "source_url": GAZETTE_URL,
                "notes": (
                    "The day is given in EACH of the three months prior to the expected "
                    "date of confinement, so the entitlement is three days in total and "
                    "one is not carried from one month into the next. Two parameters "
                    "rather than a pre-multiplied three, because the clause states two "
                    "figures and a month in which no clinic was attended pays nothing. "
                    f"{ADDITIVE} {CLAUSE_13_5}"
                ),
            },
            {
                "parameter_code": "MATERNITY_RETURN_PAYMENT_MONTH_DIVISOR",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "3.000000",
                "unit": "ratio",
                "effective_from": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 13.4(a)",
                "source_url": GAZETTE_URL,
                "notes": (
                    "On RETURN from maternity leave, payment of one third of one month's "
                    "wage at the rate the employee was on at the time of going on leave - "
                    "not the rate on return, so an increase during the leave does not "
                    "raise it. THE DIVISOR IS LOADED, NOT A THIRD: 0.333333 is a rounded "
                    "figure standing where an exact one belongs, and a third of R9 000 is "
                    "R3 000 exactly where 0.333333 x 9000 is R2 999,997. Payable on "
                    "return, so an employee who does not return does not earn it. "
                    f"{ADDITIVE} {CLAUSE_13_5}"
                ),
            },
        ]
    },
}

LEAVE_TYPE_FILENAME = "ref-2026.04.01-bccci-leave-types.json"
LEAVE_TYPE_LABEL = "REF-2026.04.01-BCCCI-LEAVE-TYPES"
LEAVE_TYPE_FROM = "2026-04-01"
LEAVE_TYPE_TO = None
STUDY_CLAUSE = "12"
STEWARD_CLAUSE = "20"
LEAVE_TYPE_DESCRIPTION = (
    "Study leave (clause 12) and shop steward leave (clause 20.4) for contract "
    "cleaning Area B under the BCCCI Main Collective Agreement, GN R.7296 in GG "
    "54412. Neither is a BCEA entitlement and neither is a per-cycle bank, so "
    "neither is accrued. NOT verified."
)


# ---------------------------------------------------------------------------
# STUDY LEAVE AND SHOP STEWARD LEAVE (D-268) - two entitlements this agreement
# CREATES, which the BCEA does not have at all. The prenatal clinic day is the
# third of them and its figures already went in with the maternity benefits
# (D-244), so they are not repeated here.
#
# Their own version rather than an addition to the rule set fixture, which is
# already loaded and checksummed: --supersede permits prose changes only
# (D-199), and a new figure is an ordinary load.
#
# NEITHER IS A PER-CYCLE BANK, which is why both are statutory_parameter rows
# and not leave_rule_set columns. Study leave is per EXAMINATION; shop steward
# leave is per YEAR but at one of two figures depending on a fact about the
# person that no column carries.
# ---------------------------------------------------------------------------

PREPARE_NOTE = (
    "One day's leave to PREPARE for each examination, on full pay. Per EXAMINATION "
    "and not per cycle: the clause gives the entitlement each time an employee "
    "writes, so there is no annual bank for the accrual engine to add to. "
    "Conditioned on satisfactory proof that the employee was allowed to write AND "
    "HAS DULY WRITTEN an examination conducted by a registered educational body; a "
    "casual employee is excluded by name. The clause also lets an employer refuse a "
    "later grant to an employee who already took study leave and failed that "
    "examination - a condition, not a figure, so it is not loaded."
)

WRITE_NOTE = (
    "One day's leave to WRITE each examination, on full pay. Loaded as its own "
    "figure rather than summed with the preparation day, because the clause states "
    "two separate entitlements and an employee who writes without preparing is owed "
    "the second and not the first. Summing them here would make one day's leave "
    "indistinguishable from two."
)

OFFICE_BEARER_NOTE = (
    "4 days' paid leave a year for AN OFFICE BEARER of a representative trade "
    "union, to attend to union affairs. THE OFFICE BEARER GETS FEWER DAYS than an "
    "ordinary shop steward, who gets 6 - which reads like the two limbs were "
    "swapped in drafting. Both agreements print it this way, word for word, three "
    "years apart, so it is transcribed as printed and flagged rather than "
    "corrected: O-32's rule is that nothing here edits a gazette into agreement "
    "with what it ought to say."
)

OTHER_STEWARD_NOTE = (
    "6 days' paid leave a year for ANY OTHER shop steward. Which of the two figures "
    "applies is a fact about the PERSON - whether they are an office bearer of a "
    "representative trade union - and no column on employee carries it, so "
    "statutory.resolve.shop_steward_leave_days() takes the status as an argument "
    "rather than guessing (D-110's shape). The union must give 14 days' written "
    "notice except in an emergency, which is a condition rather than a figure."
)


def _leave_type_parameters(study_clause: str, steward_clause: str) -> list[dict]:
    """The four figures, identical in both agreements, cited to each one's own
    clause numbers (D-260: the predecessor's numbering is one behind)."""
    specifications = (
        ("STUDY_LEAVE_PREPARE_DAYS_PER_EXAM", "1.000000", f"{study_clause}.1(a)", PREPARE_NOTE),
        ("STUDY_LEAVE_WRITE_DAYS_PER_EXAM", "1.000000", f"{study_clause}.1(b)", WRITE_NOTE),
        (
            "SHOP_STEWARD_LEAVE_DAYS_OFFICE_BEARER",
            "4.000000",
            f"{steward_clause}.4(a)(i)",
            OFFICE_BEARER_NOTE,
        ),
        (
            "SHOP_STEWARD_LEAVE_DAYS_OTHER",
            "6.000000",
            f"{steward_clause}.4(a)(ii)",
            OTHER_STEWARD_NOTE,
        ),
    )
    return [
        {
            "parameter_code": code,
            "value_numeric": value,
            "unit": "days",
            "sector": "CONTRACT_CLEANING",
            "sector_area": "AREA_B",
            "effective_from": LEAVE_TYPE_FROM,
            "effective_to": LEAVE_TYPE_TO,
            "source_reference": f"{GAZETTE}, clause {pinpoint}",
            "source_url": GAZETTE_URL,
            "notes": note,
        }
        for code, value, pinpoint, note in specifications
    ]


LEAVE_TYPE_OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / LEAVE_TYPE_FILENAME

LEAVE_TYPE_FIXTURE = {
    "version_label": LEAVE_TYPE_LABEL,
    "applies_from": LEAVE_TYPE_FROM,
    "description": LEAVE_TYPE_DESCRIPTION,
    "tables": {"statutory_parameter": _leave_type_parameters(STUDY_CLAUSE, STEWARD_CLAUSE)},
}


def main():
    for path, document in (
        (OUTPUT, FIXTURE),
        (MATERNITY_OUTPUT, MATERNITY_FIXTURE),
        (LEAVE_TYPE_OUTPUT, LEAVE_TYPE_FIXTURE),
    ):
        rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {path.relative_to(path.parents[1])}")


if __name__ == "__main__":
    main()
