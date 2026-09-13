"""Build ``reference/ref-2026.03.01-lumpsum.json`` — the two termination lump sum codes.

Loaded from SARS's own 2026 code guide, PAYE-AE-06-G06 revision 13, effective
19 September 2025. D-70 deliberately left 3901 out of the first load because nobody
had the guide; the guide arrived, so it goes in.

**3901 is what unblocks severance.** ``SEVERANCE`` has shipped inactive since the
component catalogue was built, because pointing it at 3601 would have put a
termination payment on the wrong IRP5 line and taxed it at the wrong rate (D-90).

**3907 is the sibling, and leaving it out would be the trap.** SARS's note on 3901 is
explicit: a lump sum under paragraph (d) of "gross income" that is **not** a severance
benefit MUST go under 3907. So a gratuity on resignation, or a lump sum on retirement
below 55, is 3907 — and without it loaded, the only code available would be 3901,
which is exactly the misfiling the note exists to prevent.

**The three base flags are derived, and each has its own statute.**

``is_taxable`` True
    Both are "(Subject to PAYE)" in the guide. Taxed on a directive against the
    retirement lump sum table rather than through the ordinary tables, which is a
    calculation concern rather than a flag.

``is_uif_remuneration`` False
    Unemployment Insurance Contributions Act 4 of 2002, s1: "remuneration" excludes an
    amount "which constitutes an amount contemplated in paragraph (a), (cA), (d), (e),
    (eA) or (eD) of the definition of 'gross income'". SARS's own note places both
    codes in paragraph (d). The same subsection is why commission (3606) is outside
    the UIF base, which the first load already recorded.

``is_sdl_remuneration`` False
    Skills Development Levies Act 9 of 1999, s3, as set out in SARS's SDL employer
    guide: the leviable amount excludes "amounts in terms of paragraph (a), (d), (e)
    or (eA) of the definition of 'gross income'". Paragraph (d) again.

``is_coida_remuneration`` False, AND FLAGGED
    This one is a reading rather than a citation. COIDA "earnings" for the return of
    earnings is a narrower, differently-defined concept than either of the two above,
    and no source was found that names termination lump sums directly. False is the
    reading that a severance benefit is not earnings in respect of employment during
    the assessment period. It carries VERIFY so it reaches the priority sheet and the
    labour law reviewer.
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-lumpsum.json"

GUIDE = (
    "SARS PAYE-AE-06-G06, Guide for Codes Applicable to Employees Tax Certificates "
    "2026, revision 13, effective 19 September 2025"
)
GUIDE_URL = "https://www.sars.gov.za/types-of-tax/pay-as-you-earn/"

FIXTURE = {
    "version_label": "REF-2026.03.01-LUMPSUM",
    "applies_from": "2026-03-01",
    "description": (
        "Termination lump sum source codes 3901 and 3907, from SARS's 2026 code guide. "
        "Researched 13 September 2026. NOT verified."
    ),
    "tables": {
        "sars_source_code": [
            {
                "code": "3901",
                "description": "Gratuities / Severance Benefits (Subject to PAYE)",
                "code_group": "income",
                "is_taxable": True,
                "is_uif_remuneration": False,
                "is_sdl_remuneration": False,
                "is_coida_remuneration": False,
                "source_reference": GUIDE,
                "source_url": GUIDE_URL,
                "notes": (
                    "The guide: 'Severance benefits, as defined, paid/payable by an "
                    "employer after 1 March 2011, if employee: is 55 years or older; "
                    "became permanently incapable to be employed due to ill health, "
                    "etc.; or services terminated due to reduction of personnel or "
                    "employer ceased trading.' "
                    "THE THIRD LIMB IS THE ONE THIS PRODUCT USES: 'reduction of "
                    "personnel' is dismissal for operational requirements, which is "
                    "the only ground on which BCEA s41 severance is due. It matches "
                    "EmployeeEngagement.SEVERANCE_REASONS exactly. "
                    "A lump sum on resignation, or on retirement below 55, is NOT a "
                    "severance benefit and goes to 3907 - the guide says MUST. "
                    "3951 is for foreign services income only and is not loaded. "
                    "UIF base False: UICA 4 of 2002 s1 excludes paragraph (d) gross "
                    "income amounts, and the guide places this code in paragraph (d). "
                    "SDL base False: SDLA 9 of 1999 s3 excludes paragraph (d) likewise. "
                    "COIDA base False is a READING, NOT A CITATION - VERIFY. No source "
                    "was found naming termination lump sums in the COIDA earnings "
                    "definition; False is the reading that a severance benefit is not "
                    "earnings in respect of employment during the assessment period."
                ),
            },
            {
                "code": "3907",
                "description": "Other lump sums (Subject to PAYE)",
                "code_group": "income",
                "is_taxable": True,
                "is_uif_remuneration": False,
                "is_sdl_remuneration": False,
                "is_coida_remuneration": False,
                "source_reference": GUIDE,
                "source_url": GUIDE_URL,
                "notes": (
                    "Loaded because leaving it out is the trap. The guide's note on "
                    "3901 says a lump sum under paragraph (d) of 'gross income' which "
                    "is NOT a severance benefit MUST be reflected under 3907 - so "
                    "without this code the only one available would be 3901, which is "
                    "exactly the misfiling that note exists to prevent. "
                    "The guide's examples: 'A lump sum payment paid/payable by an "
                    "employer due to normal termination of service (e.g., resignation "
                    "or retirement), which is NOT a severance benefit' and 'Gratuity "
                    "paid to an employee due to normal termination of service (e.g., "
                    "resignation or a lump sum paid upon retirement where employee is "
                    "below 55 years of age).' "
                    "3957 is for foreign services income only and is not loaded. "
                    "Base flags derived exactly as for 3901, same paragraph (d), same "
                    "two statutes. COIDA base False is a READING - VERIFY."
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
