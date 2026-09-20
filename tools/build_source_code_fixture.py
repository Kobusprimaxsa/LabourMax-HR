"""Builds the SARS source code fixture — the codes these two sectors actually use.

Fifteen codes, not the several hundred SARS publishes. A domestic employer and a
contract cleaning company between them pay salary, overtime, a December bonus, the
odd allowance and one or two fringe benefits, and deduct PAYE, UIF and SDL. Loading
the share-scheme and foreign-service codes would be loading four flags apiece that
nobody has reasoned about, into a table whose whole purpose is that somebody has.

**The four base flags are the point of this table, and they are readings rather than
transcriptions.** SARS gives the code and its wording; whether an amount enters the
UIF, SDL or COIDA base comes from three different statutes with three different
definitions. Commission is the standing proof: taxable, inside the SDL base, and
outside the UIF base. So every row carries its reasoning in ``notes``, and the two
readings that are genuinely uncertain say VERIFY in capitals.

What is deliberately absent: 3901 severance, because severance is taxed on the
retirement lump sum tables rather than the ordinary ones and belongs with the
termination work in P6; 3616 independent contractors, whose UIF treatment turns on
facts about the person rather than the code; and every travel, share and foreign
code, none of which arises in these sectors.

Run: python tools/build_source_code_fixture.py > reference/ref-2026.03.01-codes.json
"""

from __future__ import annotations

import json
import pathlib

SARS_GUIDE = (
    "SARS Guide for Codes Applicable to Employees Tax Certificates "
    "(PAYE-AE-06-G06), 2026 issue, revision 13 effective 19 September 2025"
)
SARS_GUIDE_URL = (
    "https://www.sars.gov.za/wp-content/uploads/Ops/Guides/"
    "PAYE-AE-06-G06-Guide-for-Codes-Applicable-to-Employees-Tax-Certificates-2026-"
    "External-Guide.pdf"
)

UIF_BASE = (
    "UIF base: Unemployment Insurance Contributions Act 4 of 2002, s6 read with the "
    "s1 definition of remuneration."
)
SDL_BASE = (
    "SDL base: Skills Development Levies Act 9 of 1999, s3, on the leviable amount - "
    "remuneration as defined in the Fourth Schedule to the Income Tax Act."
)
COIDA_BASE = (
    "COIDA base: Compensation for Occupational Injuries and Diseases Act 130 of 1993, "
    "s1 definition of earnings."
)

NOT_A_PAY_LINE = (
    "A total or a deduction, not a pay line. All four base flags are False because "
    "nothing is ever calculated FROM this code - it is calculated INTO it. A "
    "calculator that reads a base flag here has asked the wrong question."
)

DESCRIPTION_NOT_VERBATIM = (
    "VERIFY THE WORDING before P8 generates an IRP5. The description here is the "
    "conventional one; it was not transcribed character for character from the SARS "
    "guide, which gives full wording only for selected codes."
)


def code(
    *,
    number,
    description,
    group,
    taxable,
    uif,
    sdl,
    coida,
    notes,
    source=SARS_GUIDE,
):
    return {
        "code": number,
        "description": description,
        "code_group": group,
        "is_taxable": taxable,
        "is_uif_remuneration": uif,
        "is_sdl_remuneration": sdl,
        "is_coida_remuneration": coida,
        "source_reference": source,
        "source_url": SARS_GUIDE_URL,
        "notes": notes,
    }


DOCUMENT = {
    "version_label": "REF-2026.03.01-CODES-r2",
    "applies_from": "2026-03-01",
    "description": (
        "SARS source codes used by the domestic and contract cleaning sectors, with "
        "the PAYE, UIF, SDL and COIDA base flags and the reasoning behind each. "
        "Researched 13 September 2026. NOT verified."
    ),
    "tables": {
        "sars_source_code": [
            code(
                number="3601",
                description="Income (Subject to PAYE)",
                group="income",
                taxable=True,
                uif=True,
                sdl=True,
                coida=True,
                notes=(
                    f"Ordinary salary and wages - the code carrying most of every "
                    f"payslip in both sectors. In all four bases. {UIF_BASE} {SDL_BASE} "
                    f"{COIDA_BASE}"
                ),
            ),
            code(
                number="3602",
                description="Income (Non-taxable)",
                group="income",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=(
                    "Outside the Fourth Schedule definition of remuneration, so outside "
                    "the PAYE, UIF and SDL bases that are built on it."
                ),
            ),
            code(
                number="3605",
                description="Annual payment (Subject to PAYE)",
                group="income",
                taxable=True,
                uif=True,
                sdl=True,
                coida=True,
                notes=(
                    f"The December bonus, and the contract cleaning sector's 4,333-week "
                    f"statutory bonus. In every base - bonuses are NOT excluded from UIF, "
                    f"unlike commission. The classic PAYE error lives here: annualise "
                    f"regular pay by twelve, then add the annual payment ONCE. "
                    f"Multiplying the bonus by twelve is the December over-deduction. "
                    f"{UIF_BASE}"
                ),
            ),
            code(
                number="3606",
                description="Commission (Subject to PAYE)",
                group="income",
                taxable=True,
                uif=False,
                sdl=True,
                coida=True,
                notes=(
                    f"THE ROW THIS TABLE EXISTS FOR. Commission is taxable, is inside "
                    f"the SDL leviable amount, and is OUTSIDE the UIF contribution base. "
                    f"A single is_taxable flag would get that wrong silently, and the "
                    f"error surfaces at a UI-19 reconciliation months later. {UIF_BASE} "
                    f"The exclusion is confirmed by the Payroll Authors Group of South "
                    f"Africa; VERIFY the exact paragraph of the s1 definition against "
                    f"the Act before this version is signed off."
                ),
            ),
            code(
                number="3607",
                description="Overtime (Subject to PAYE)",
                group="income",
                taxable=True,
                uif=True,
                sdl=True,
                coida=True,
                notes=(
                    f"VERIFY THE COIDA FLAG. Overtime is plainly taxable and plainly in "
                    f"the UIF and SDL bases. COIDA's definition of earnings treats "
                    f"overtime differently depending on whether it is regular or casual, "
                    f"and this row takes the inclusive reading, which over-declares "
                    f"rather than under-declares on the Return of Earnings. A wrong "
                    f"answer here costs an assessment, not a payslip. One for the labour "
                    f"law review (O-06). {COIDA_BASE}"
                ),
            ),
            code(
                number="3713",
                description="Other allowances (Subject to PAYE)",
                group="allowance",
                taxable=True,
                uif=True,
                sdl=True,
                coida=True,
                notes=(
                    "Where the sectoral allowances land: SD1's night allowance of 10 "
                    "percent of the hourly wage, and SD7's standby allowance per shift. "
                    "Both are remuneration in the ordinary sense and sit in every base."
                ),
            ),
            code(
                number="3714",
                description="Other allowances (Non-taxable)",
                group="allowance",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes="Non-taxable by definition, and therefore outside the bases built on it.",
            ),
            code(
                number="3801",
                description="General fringe benefits (Subject to PAYE)",
                group="fringe_benefit",
                taxable=True,
                uif=True,
                sdl=True,
                coida=True,
                notes=(
                    "The domestic sector's live case is accommodation. SD7 permits a "
                    "deduction of up to 10 percent of the wage for a room that meets its "
                    "standard - a DEDUCTION from pay, which is a different thing from "
                    "the taxable value of a benefit, and the two must not be netted "
                    "against each other on a payslip."
                ),
            ),
            code(
                number="3810",
                description="Medical aid contributions (Subject to PAYE)",
                group="fringe_benefit",
                taxable=True,
                uif=True,
                sdl=True,
                coida=True,
                notes=(
                    "The employer's contribution to a medical scheme, taxed as a fringe "
                    "benefit. Rare in both sectors, and the medical scheme fees tax "
                    "credit that offsets it is code 4116."
                ),
            ),
            code(
                number="3696",
                description="Gross non-taxable income",
                group="total",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=NOT_A_PAY_LINE,
            ),
            code(
                number="3699",
                description="Gross employment income (taxable)",
                group="total",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=NOT_A_PAY_LINE,
            ),
            code(
                number="4001",
                description="Current pension fund contributions",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=f"{NOT_A_PAY_LINE} {DESCRIPTION_NOT_VERBATIM}",
            ),
            code(
                number="4003",
                description="Current and arrear provident fund contributions",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=(
                    f"The contract cleaning sector has a national provident fund under "
                    f"SD1 clause 32, so this code carries real traffic there. Its "
                    f"contribution rates are not loaded - they are not in this table's "
                    f"scope and were not researched. {NOT_A_PAY_LINE} "
                    f"{DESCRIPTION_NOT_VERBATIM}"
                ),
            ),
            code(
                number="4005",
                description="Medical scheme contributions (employee)",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=f"{NOT_A_PAY_LINE} {DESCRIPTION_NOT_VERBATIM}",
            ),
            code(
                number="4102",
                description="PAYE",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=f"{NOT_A_PAY_LINE} {DESCRIPTION_NOT_VERBATIM}",
            ),
            code(
                number="4116",
                description="Medical scheme fees tax credit",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=(
                    f"Offsets PAYE rather than reducing income, which is why it is a "
                    f"credit and not a deduction from remuneration. {NOT_A_PAY_LINE} "
                    f"{DESCRIPTION_NOT_VERBATIM}"
                ),
            ),
            code(
                number="4141",
                description="UIF contribution (employee and employer)",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=(
                    f"Carries BOTH contributions - the employee's one percent and the "
                    f"employer's - as a single figure on the certificate. Reporting only "
                    f"the employee half is a common reconciliation error. "
                    f"{NOT_A_PAY_LINE} {DESCRIPTION_NOT_VERBATIM}"
                ),
            ),
            code(
                number="4142",
                description="SDL contribution",
                group="deduction",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=(
                    f"An employer liability, never deducted from the employee. "
                    f"{NOT_A_PAY_LINE} {DESCRIPTION_NOT_VERBATIM}"
                ),
            ),
            code(
                number="4149",
                description="Total tax, UIF and SDL",
                group="total",
                taxable=False,
                uif=False,
                sdl=False,
                coida=False,
                notes=f"{NOT_A_PAY_LINE} {DESCRIPTION_NOT_VERBATIM}",
            ),
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


OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01-codes.json"


def main():
    OUTPUT.write_text(json.dumps(DOCUMENT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
