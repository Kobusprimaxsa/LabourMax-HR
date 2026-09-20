"""Builds the bank fixture — universal branch codes for salary payments.

Not statutory, and deliberately not a cited statutory table: a universal branch code
is published by the bank itself, not gazetted, and demanding a gazette citation for
one invites a fabricated citation. Each row records its provenance in ``notes``
instead.

**No account number lengths are loaded.** They are left NULL, which now means "nobody
has confirmed this bank's rule". Per-bank lengths come from the banks and from the
ACB specification rather than from one published list, and the only figure findable
in public is the general nine-to-eleven digit range. Guessing a narrower bound would
reject valid account numbers, which reads to an employer as a system fault and ends
with somebody not being paid on the 25th. When Kobus confirms a bank's rule - from
the bank, or from the EFT specification the payment partner supplies - it loads as
its own version.

Banks omitted on purpose: VBS Mutual Bank (liquidated), The Royal Bank of Scotland
N.V. (exited South Africa), MTN Banking (closed), and three names on the source list
whose current status could not be confirmed - Olympus Mobile, People Bank and
Permanent Bank. An inactive bank in this table is worse than a missing one: it offers
an employer a choice that will bounce the payment.

Run: python tools/build_bank_fixture.py > reference/ref-2026.03.01-banks.json
"""

from __future__ import annotations

import json
import pathlib

SOURCE = "Universal branch codes as published by the banks, cross-checked 13 September 2026"

RETAIL = "Cross-checked against two independent published lists."

CORPORATE = (
    "Corporate or foreign branch. Present so an employee banking there can be paid, "
    "not because either sector's employees commonly do."
)

BANKS = [
    ("Absa Bank Limited", "632005", RETAIL),
    ("African Bank Limited", "430000", RETAIL),
    ("Access Bank (South Africa) Limited", "410105", "Single source - VERIFY."),
    ("Albaraka Bank Limited", "800000", "Single source - VERIFY."),
    ("Bank Zero Mutual Bank", "888000", RETAIL),
    ("Bidvest Bank Limited", "462005", RETAIL),
    ("Capitec Bank Limited", "470010", RETAIL),
    ("Capitec Business", "450105", "Formerly Mercantile Bank. Single source - VERIFY."),
    ("Discovery Bank Limited", "679000", RETAIL),
    ("Finbond Mutual Bank", "589000", "Single source - VERIFY."),
    (
        "FirstRand Bank Limited (FNB)",
        "250655",
        f"{RETAIL} FNB is a division of FirstRand Bank; employees will say 'FNB'.",
    ),
    ("GoTyme Bank Limited", "678910", "Rebranded from TymeBank in January 2026. Code unchanged."),
    ("Grindrod Bank Limited", "584000", "Single source - VERIFY."),
    ("Investec Bank Limited", "580105", RETAIL),
    ("Nedbank Limited", "198765", RETAIL),
    ("Sasfin Bank Limited", "683000", "Single source - VERIFY."),
    ("South African Postbank", "460005", "Single source - VERIFY."),
    ("Standard Bank of South Africa", "051001", RETAIL),
    ("Bank of China", "686000", CORPORATE),
    ("China Construction Bank", "586666", CORPORATE),
    ("HSBC Bank", "587000", CORPORATE),
    ("ICICI Bank Limited", "362000", CORPORATE),
    ("JP Morgan Chase Bank N.A.", "432000", CORPORATE),
    ("Societe Generale", "351005", CORPORATE),
    ("State Bank of India", "801000", CORPORATE),
]

LENGTHS_UNKNOWN = (
    "Account number length not loaded: NULL means nobody has confirmed this bank's "
    "rule. Validation falls back to the general nine-to-eleven digit range."
)


DOCUMENT = {
    "version_label": "REF-2026.03.01-BANKS",
    "applies_from": "2026-03-01",
    "description": (
        "South African banks and their universal branch codes, for payslip display and "
        "EFT file validation. Account number lengths deliberately not loaded. "
        "Compiled 13 September 2026. NOT verified."
    ),
    "tables": {
        "bank": [
            {
                "name": name,
                "universal_branch_code": branch_code,
                "notes": f"{note} {SOURCE}. {LENGTHS_UNKNOWN}",
            }
            for name, branch_code, note in BANKS
        ]
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


OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-banks.json"


def main():
    OUTPUT.write_text(json.dumps(DOCUMENT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
