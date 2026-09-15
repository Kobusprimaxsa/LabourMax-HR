"""Builds the notice band fixture — D-68, closed; D-158 corrected.

Replaces ``termination_rule_set``'s three ``notice_weeks_*`` columns, which
could not express SD1's boundary at FOUR WEEKS of service or its unit (one
WORKING DAY, worth a fifth or a sixth of a week depending on the working
week). The BCEA and domestic figures below are the SAME figures those three
columns held — TRANSCRIBED from ``reference/ref-2026.03.01-rules.json``
before this fixture existed, not retyped from memory — so nothing about the
BCEA default or the domestic sector's SD7 override moved. Only SD1's two
bands, from clause 23(1), are new.

    BCEA s37(1): (a) one week, employed for SIX MONTHS OR LESS; (b) two
        weeks, employed for MORE THAN six months but NOT MORE THAN one year;
        (c) four weeks, employed for ONE YEAR OR MORE, or a farm/domestic
        worker employed for MORE THAN six months.
    SD1 clause 23(1): (a) DURING THE FIRST FOUR WEEKS, not less than one
        working day's; (b) four weeks, employed for MORE THAN four weeks.

**Inclusivity is DATA, per boundary, read from each statute's own words**
(D-158, corrected — the first version of this fixture put every exact
boundary in the upper band, which BCEA s37(1)(a) and SD1 clause 23(1)(a)
both contradict directly: "six months OR LESS" and "the FIRST FOUR WEEKS"
both claim the boundary for the LOWER band). The one genuine exception is
BCEA's one-year mark, where (b)'s "not more than one year" and (c)(i)'s "one
year or more" both name the same instant — an actual overlap in the Act's
own text, not a modelling choice, resolved here by keeping four weeks (the
richer entitlement) and flagged for the labour law review (O-06) rather than
asserted as obviously correct.

``service_from_inclusive`` is true on every band's own docstring-visible
first appearance in this file except where the band directly below it has
already claimed that same instant; ``service_to_inclusive`` is read straight
off the clause that states the boundary, not inferred.

Domestic collapses to TWO bands, not three: its 6-months-and-over and
over-1-year figures are both 4 weeks (SD7's own acceleration), so a third
band would be redundant with the second rather than a genuine boundary.

Run: python tools/build_notice_band_fixture.py > reference/ref-2026.03.01-notice-bands.json
"""

from __future__ import annotations

import json

BCEA = "Basic Conditions of Employment Act 75 of 1997"
SD7 = (
    "Sectoral Determination 7: Domestic Worker Sector, published in Regulation "
    "Gazette No. 7434 (consolidated text), read with Basic Conditions of Employment "
    "Act 75 of 1997 s37(1)(c)"
)
SD1 = (
    "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text "
    "(clauses 3, 8-24)"
)

EFFECTIVE_FROM = "2026-03-01"

CARRIED_OVER_NOTE = (
    "Carried over from termination_rule_set.notice_weeks_* (D-68) — the same figure, "
    "the same citation, now with its own unit instead of an implied week."
)

TOUCH_NOTE = (
    "service_from/service_to touch the adjacent band exactly, in the same unit both "
    "sides, by construction — the reconciliation check in statutory/checks.py relies "
    "on that rather than converting between units."
)


def band(
    *,
    sequence,
    source,
    from_value,
    from_unit,
    from_inclusive,
    to_value,
    to_unit,
    to_inclusive,
    notice_value,
    notice_unit,
    sector=None,
    notes="",
):
    row = {
        "sector": sector,
        "effective_from": EFFECTIVE_FROM,
        "sequence": sequence,
        "source_reference": source,
        "notes": notes,
        "service_from_value": from_value,
        "service_from_unit": from_unit,
        "service_from_inclusive": from_inclusive,
        "service_to_value": to_value,
        "service_to_unit": to_unit,
        "service_to_inclusive": to_inclusive,
        "notice_value": notice_value,
        "notice_unit": notice_unit,
    }
    return row


DOCUMENT = {
    "version_label": "REF-2026.03.01-NOTICE-BANDS-2",
    "applies_from": "2026-03-01",
    "description": (
        "termination_notice_band (D-68, D-158 corrected): the BCEA default and the "
        "domestic sector's SD7 override, carried over unchanged from "
        "termination_rule_set's retired notice_weeks_* columns; SD1's own two-band "
        "notice regime, clause 23(1); and per-boundary inclusivity read from each "
        "statute's own wording rather than a single global convention. NOT verified."
    ),
    "tables": {
        "termination_notice_band": [
            # --- BCEA default: s37(1). Three bands, three distinct figures.
            band(
                sequence=1,
                source=f"{BCEA}, s37(1)(a)",
                from_value="0",
                from_unit="months",
                from_inclusive=True,
                to_value="6",
                to_unit="months",
                to_inclusive=True,  # "six months OR LESS" — the boundary is this band's.
                notice_value="1",
                notice_unit="weeks",
                notes=(
                    f"{CARRIED_OVER_NOTE} 's37(1)(a): one week, if the employee has been "
                    f"employed for six months or less'. Upper boundary INCLUSIVE — the "
                    f"statute's own words put exactly six months here, not in band 2. "
                    f"{TOUCH_NOTE}"
                ),
            ),
            band(
                sequence=2,
                source=f"{BCEA}, s37(1)(b)",
                from_value="6",
                from_unit="months",
                from_inclusive=False,  # band 1 already claims exactly six months.
                to_value="1",
                to_unit="years",
                to_inclusive=False,  # (c)(i) claims exactly one year instead.
                notice_value="2",
                notice_unit="weeks",
                notes=(
                    f"{CARRIED_OVER_NOTE} 's37(1)(b): two weeks, if employed for more "
                    f"than six months but not more than one year'. Both boundaries "
                    f"EXCLUSIVE: six months belongs to band 1 ('or less'), and one year "
                    f"belongs to band 3 below — see that band's own note on the genuine "
                    f"overlap in the Act's text at exactly one year (O-06)."
                ),
            ),
            band(
                sequence=3,
                source=f"{BCEA}, s37(1)(c)(i)",
                from_value="1",
                from_unit="years",
                from_inclusive=True,  # "one year OR MORE".
                to_value=None,
                to_unit="",
                to_inclusive=None,
                notice_value="4",
                notice_unit="weeks",
                notes=(
                    f"{CARRIED_OVER_NOTE} 's37(1)(c)(i): four weeks, if the employee has "
                    f"been employed for one year or more'. GENUINE OVERLAP IN THE ACT "
                    f"(O-06): s37(1)(b) says 'not more than one year' and this clause "
                    f"says 'one year or more' — both name exactly one year of service. "
                    f"Four weeks (the richer entitlement) is the reading loaded here, "
                    f"not a reading of what the boundary itself unambiguously says, "
                    f"because there is no such single reading — flagged for the labour "
                    f"law review rather than asserted as settled."
                ),
            ),
            # --- Domestic: SD7 accelerates to four weeks at six months, and does not
            # escalate again at one year, so two bands, not three.
            band(
                sequence=1,
                sector="DOMESTIC",
                source=SD7,
                from_value="0",
                from_unit="months",
                from_inclusive=True,
                to_value="6",
                to_unit="months",
                to_inclusive=True,  # BCEA s37(1)(a)'s "or less", read with s37(1)(c)(ii).
                notice_value="1",
                notice_unit="weeks",
                notes=(
                    f"{CARRIED_OVER_NOTE} Upper boundary INCLUSIVE, same reading as the "
                    f"BCEA default's own band 1 — six months or less is one week "
                    f"regardless of sector. {TOUCH_NOTE}"
                ),
            ),
            band(
                sequence=2,
                sector="DOMESTIC",
                source=SD7,
                from_value="6",
                from_unit="months",
                from_inclusive=False,  # band 1 already claims exactly six months.
                to_value=None,
                to_unit="",
                to_inclusive=None,
                notice_value="4",
                notice_unit="weeks",
                notes=(
                    f"{CARRIED_OVER_NOTE} BCEA s37(1)(c)(ii): four weeks for a domestic "
                    f"worker employed for MORE THAN six months — exclusive, unlike the "
                    f"ordinary s37(1)(c)(i) one-year mark, because this clause states "
                    f"its own threshold at six months with no companion clause "
                    f"contesting it. SD7's 6-months-and-over and over-1-year columns "
                    f"held the same figure (4 weeks), so this is one open-ended band "
                    f"rather than a redundant third one with no real boundary."
                ),
            ),
            # --- Contract cleaning: SD1 clause 23(1), read for this closure.
            band(
                sequence=1,
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clause 23(1)(a)",
                from_value="0",
                from_unit="weeks",
                from_inclusive=True,
                to_value="4",
                to_unit="weeks",
                to_inclusive=True,  # "DURING THE FIRST FOUR WEEKS" — inclusive of week 4.
                notice_value="1",
                notice_unit="days",
                notes=(
                    f"'during the first four weeks of employment, not less than one "
                    f"working day's [notice]'. A DAY, not a fraction of a week — see "
                    f"the model docstring. Upper boundary INCLUSIVE: 'the first four "
                    f"weeks' names exactly four weeks as still inside it. {TOUCH_NOTE}"
                ),
            ),
            band(
                sequence=2,
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clause 23(1)(b)",
                from_value="4",
                from_unit="weeks",
                from_inclusive=False,  # band 1 already claims exactly four weeks.
                to_value=None,
                to_unit="",
                to_inclusive=None,
                notice_value="4",
                notice_unit="weeks",
                notes=(
                    "'four weeks, if the employee has been employed for more than four "
                    "weeks'. EXCLUSIVE lower boundary: 'more than four weeks' is the "
                    "Act's own word for it, and band 1 above already holds exactly four "
                    "weeks inclusively (D-158, corrected — the first version of this "
                    "fixture had this the other way round)."
                ),
            ),
        ],
    },
}


if __name__ == "__main__":
    print(json.dumps(DOCUMENT, indent=2, ensure_ascii=False))
