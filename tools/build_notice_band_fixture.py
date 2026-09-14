"""Builds the notice band fixture — D-68, closed.

Replaces ``termination_rule_set``'s three ``notice_weeks_*`` columns, which
could not express SD1's boundary at FOUR WEEKS of service or its unit (one
WORKING DAY, worth a fifth or a sixth of a week depending on the working
week). The BCEA and domestic figures below are the SAME figures those three
columns held — TRANSCRIBED from ``reference/ref-2026.03.01-rules.json``
before this fixture existed, not retyped from memory — so nothing about the
BCEA default or the domestic sector's SD7 override moved. Only SD1's two
bands, from clause 23(1), are new.

    SD1 clause 23(1)(a): "during the first four weeks of employment, not
        less than one working day's [notice]"
    SD1 clause 23(1)(b): "four weeks, if the employee has been employed for
        more than four weeks."

**An exact boundary resolves to the band that STARTS there** (D-158), the
same inclusive-start/exclusive-end convention every effective-dated range in
this schema already uses. Read most literally, clause 23(1)(b)'s "more than
four weeks" would put exactly-four-weeks in band (a) instead — this fixture
does not follow that literal reading, for two reasons recorded in D-158: it
keeps every boundary in this table (and every other effective-dated range in
the schema) resolving the same way, and it is the direction this codebase
already favours when a boundary is genuinely a judgement call — the existing
SD1 termination row's own retired note called an over-generous notice period
"a cost", and an under-generous one "a compliance breach". Flagged for the
labour law review (O-06) in case it disagrees.

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
    to_value,
    to_unit,
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
        "service_to_value": to_value,
        "service_to_unit": to_unit,
        "notice_value": notice_value,
        "notice_unit": notice_unit,
    }
    return row


DOCUMENT = {
    "version_label": "REF-2026.03.01-NOTICE-BANDS",
    "applies_from": "2026-03-01",
    "description": (
        "termination_notice_band (D-68): the BCEA default and the domestic sector's "
        "SD7 override, carried over unchanged from termination_rule_set's retired "
        "notice_weeks_* columns; and SD1's own two-band notice regime, clause 23(1), "
        "read for the first time here. NOT verified."
    ),
    "tables": {
        "termination_notice_band": [
            # --- BCEA default: ss 37 and 41. Three bands, three distinct figures.
            band(
                sequence=1,
                source=f"{BCEA}, ss 37 and 41",
                from_value="0",
                from_unit="months",
                to_value="6",
                to_unit="months",
                notice_value="1",
                notice_unit="weeks",
                notes=f"{CARRIED_OVER_NOTE} {TOUCH_NOTE}",
            ),
            band(
                sequence=2,
                source=f"{BCEA}, ss 37 and 41",
                from_value="6",
                from_unit="months",
                to_value="1",
                to_unit="years",
                notice_value="2",
                notice_unit="weeks",
                notes=CARRIED_OVER_NOTE,
            ),
            band(
                sequence=3,
                source=f"{BCEA}, ss 37 and 41",
                from_value="1",
                from_unit="years",
                to_value=None,
                to_unit="",
                notice_value="4",
                notice_unit="weeks",
                notes=CARRIED_OVER_NOTE,
            ),
            # --- Domestic: SD7 accelerates to four weeks at six months, and does not
            # escalate again at one year, so two bands, not three.
            band(
                sequence=1,
                sector="DOMESTIC",
                source=SD7,
                from_value="0",
                from_unit="months",
                to_value="6",
                to_unit="months",
                notice_value="1",
                notice_unit="weeks",
                notes=f"{CARRIED_OVER_NOTE} {TOUCH_NOTE}",
            ),
            band(
                sequence=2,
                sector="DOMESTIC",
                source=SD7,
                from_value="6",
                from_unit="months",
                to_value=None,
                to_unit="",
                notice_value="4",
                notice_unit="weeks",
                notes=(
                    f"{CARRIED_OVER_NOTE} SD7's 6-months-and-over and over-1-year "
                    f"columns held the same figure (4 weeks), so this is one open-ended "
                    f"band rather than a redundant third one with no real boundary."
                ),
            ),
            # --- Contract cleaning: SD1 clause 23(1), read for this closure. NEW.
            band(
                sequence=1,
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clause 23(1)(a)",
                from_value="0",
                from_unit="weeks",
                to_value="4",
                to_unit="weeks",
                notice_value="1",
                notice_unit="days",
                notes=(
                    f"'during the first four weeks of employment, not less than one "
                    f"working day's [notice]'. A DAY, not a fraction of a week — see "
                    f"the model docstring. {TOUCH_NOTE}"
                ),
            ),
            band(
                sequence=2,
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clause 23(1)(b)",
                from_value="4",
                from_unit="weeks",
                to_value=None,
                to_unit="",
                notice_value="4",
                notice_unit="weeks",
                notes=(
                    "'four weeks, if the employee has been employed for more than four "
                    "weeks'. D-158: exactly four weeks resolves HERE, not to band 1 — "
                    "see the module docstring for why that is not the most literal "
                    "reading of clause (b) alone."
                ),
            ),
        ],
    },
}


if __name__ == "__main__":
    print(json.dumps(DOCUMENT, indent=2, ensure_ascii=False))
