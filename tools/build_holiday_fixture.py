"""Builds the public holiday correction fixture: the Sundays s2(1) never took
away, and the 4 November 2026 election day.

**Two separate things, one fixture, because both are the same table and both
were missing on the same day** (D-280).

**The Sundays.** Public Holidays Act 36 of 1994 s2(1) reads: "The days
mentioned in Schedule 1 shall be public holidays, and whenever any public
holiday falls on a Sunday, the following Monday shall be a public holiday."
That ADDS the Monday. It does not move the holiday off the Sunday — Schedule 1
fixes the date, 9 August is National Women's Day, and nothing in s2(1) takes
that away. gov.za lists both days. ``tools/build_reference_fixture.py`` read it
as a move and emitted one row, dated the Monday, so three Sundays in the loaded
corpus were not public holidays at all.

**The election day.** Proclamation Notice 346 of 2026, in terms of s2A,
declares 4 November 2026 a public holiday throughout the Republic in connection
with the local government elections. It is a s2A day and not a Schedule 1 one,
which is what ``is_statutory=False`` is for — this is the first row in the
corpus to use it.

**The Sunday rows are DERIVED, never typed.** This builder asks the corrected
``public_holidays()`` for the calendar and keeps the rows that fall on a
Sunday, so the fix to the generator and the rows loaded from it cannot
disagree. Typing "2026-08-09, 2027-03-21, 2027-12-26" here would be a second
answer to a question the generator already answers, and a second answer is one
that drifts.

**These three rows are in ``ref-2026.03.01.json`` too, and that is not an
accident.** Two guards meet here. The builder must reproduce the shipped
fixture byte for byte, so correcting ``public_holidays()`` puts the Sundays
into the base file and its label goes to ``-r4``. But ``--supersede`` is a
RE-ENCODING and may not add a row (D-199), so on a database that already holds
``-r3`` the Sundays have to exist before that supersede runs — which is what
this file is for. Loading it twice is a no-op either way round: the loader
reports them already present. On a clean clone ``--all`` loads the base file
first and this one adds only the election day.

Run: python tools/build_holiday_fixture.py
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from build_reference_fixture import public_holidays  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = REPO / "reference" / "ref-2026.03.01-holidays.json"

YEARS = (2026, 2027)

ELECTION_CITATION = (
    "Proclamation Notice 346 of 2026, GG 55352, 8 September 2026 "
    "(Public Holidays Act 36 of 1994, s2A)"
)
ELECTION_URL = "https://www.gov.za/sites/default/files/gcis_document/202609/55352-rg12046pr346.pdf"

#: Read off the gazette itself. The PDF's body is a SCAN — it carries no text
#: layer, so ``fetchsources`` and any text extraction get the running header
#: and nothing else — and the operative wording below was read by rendering
#: page 3 as an image. That is a transcription, which is exactly what the
#: verification pass exists to check, so it is quoted here in full and the
#: workbook sends Kobus to the same page.
ELECTION_WORDING = (
    "Read off the gazette page (rendered from the scan; the PDF carries no text "
    "layer). PROCLAMATION NOTICE 346 OF 2026, By the President of the Republic of "
    'South Africa: "DECLARATION OF THE FOURTH DAY OF NOVEMBER 2026 AS A PUBLIC '
    "HOLIDAY THROUGHOUT THE REPUBLIC. In terms of Section 2A of the Public "
    "Holidays Act, 1994 (Act No. 36 of 1994), I hereby declare the Fourth day of "
    "November 2026 as a public holiday throughout the Republic in connection with "
    'the holding of local government elections." Signed at Johannesburg on the 4th '
    "day of September 2026 by the President, by order of the President-in-Cabinet. "
    "is_statutory is FALSE because this is a s2A proclamation rather than a "
    "Schedule 1 day — it is a public holiday for every BCEA purpose all the same, "
    "and nothing in this build reads the flag to decide holiday treatment."
)


def sunday_rows() -> list[dict]:
    """Every row the corrected generator emits for a date that IS a Sunday.

    Selected by the rule rather than by date, so this stays right if the
    calendar is extended to another year: a Sunday holiday is a row s2(1) adds
    a Monday to, and the Sunday is the one the old generator dropped.
    """
    return [
        row
        for year in YEARS
        for row in public_holidays(year)
        if datetime.date.fromisoformat(row["holiday_date"]).weekday() == 6
    ]


def election_day() -> dict:
    return {
        "holiday_date": "2026-11-04",
        "name": "Local government elections",
        "is_statutory": False,
        "source_reference": ELECTION_CITATION,
        "source_url": ELECTION_URL,
        "notes": ELECTION_WORDING,
    }


def build() -> dict:
    rows = sunday_rows()
    if not rows:
        raise SystemExit(
            "public_holidays() emits no Sunday row at all, which means it has "
            "regressed to treating s2(1) as a move (D-280)."
        )
    return {
        "version_label": "REF-2026.03.01-HOLIDAYS",
        # The 1 March 2026 corpus's own date, not today's: these rows correct
        # the calendar loaded THEN, and the payroll gate reads applicable
        # versions by applies_from. Dated 8 September it would not be
        # applicable to an August 2026 run, which is the run 9 August is in.
        "applies_from": "2026-03-01",
        "description": (
            "Public holidays the 1 March 2026 corpus is missing. THREE SUNDAYS: "
            "s2(1) ADDS the following Monday, it does not move the holiday off the "
            "Sunday, and the generator read it as a move (D-280). ONE PROCLAIMED "
            "DAY: 4 November 2026, s2A, the local government elections. "
            "Researched 24 September 2026. NOT verified."
        ),
        "tables": {"public_holiday": [*rows, election_day()]},
    }


def main():
    OUTPUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
