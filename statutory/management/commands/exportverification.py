"""``python manage.py exportverification`` — the workbook a person verifies from.

P2's remaining work is 589 figures that somebody has to read off a gazette, and
it has not been started because a list of version labels tells nobody what to
open. This writes the job down: grouped by source document, so each gazette is
opened once, with a summary sheet that answers "where am I" in five seconds.

The workbook is a WORK AID and never a source of truth — see
``statutory/verification.py`` and D-251.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from statutory import verification

try:  # pragma: no cover - openpyxl is a hard dependency of this command only
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
except ModuleNotFoundError as exc:  # pragma: no cover
    raise CommandError("openpyxl is required: pip install openpyxl") from exc

HEADERS = [
    ("Source document", 52),
    ("Clause / section", 30),
    ("Source URL", 30),
    ("Reference version", 30),
    ("Table", 24),
    ("What it is", 56),
    ("Value as loaded", 22),
    ("Effective from", 14),
    ("Effective to", 14),
    ("Row key", 30),
    ("Checked", 11),
    ("Checked by", 24),
    ("Date checked", 14),
    ("Note", 60),
]

CHECKED_COLUMN = 11  # 1-based, and the conditional formatting keys on it.

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
CHECKED_FILL = PatternFill("solid", fgColor="D5EAD3")
QUERY_FILL = PatternFill("solid", fgColor="FBE2C7")
MACHINE_FILL = PatternFill("solid", fgColor="DCE6F5")
DOCUMENT_FILL = PatternFill("solid", fgColor="EDEDED")


class Command(BaseCommand):
    help = "Export every loaded reference figure to a verification workbook."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default="reference/verification/Labourmax_verification.xlsx",
            help="Where to write the workbook.",
        )
        parser.add_argument(
            "--verifier",
            default="",
            help="Email of the person who will do the checking. Marks on the summary "
            "sheet which versions they may NOT verify because they loaded them, so "
            "they find out before spending an evening on those rows.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite an existing workbook. Refuses without this, because "
            "overwriting one is throwing away somebody's evening.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if path.exists() and not options["force"]:
            raise CommandError(
                f"{path} already exists. Re-exporting replaces it and every tick in it "
                f"— the database does not store them. Pass --force if that is what you "
                f"want, or export to a new filename."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = verification.lines()
        versions = verification.version_state()
        machine = {v["label"] for v in versions if v["machine_verified"]}

        book = Workbook()
        self._summary_sheet(book.active, lines, versions, options["verifier"].strip())
        self._figures_sheet(book.create_sheet("Figures"), lines, machine)
        book.save(path)

        self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
        self.stdout.write(
            f"  {len(lines)} figures across {len({x.document for x in lines})} source documents"
        )
        self.stdout.write(f"  {len(versions)} reference versions, {len(machine)} machine-verified")

    # ------------------------------------------------------------------ sheets

    def _summary_sheet(self, sheet, lines, versions, verifier):
        sheet.title = "Summary"
        sheet.column_dimensions["A"].width = 62
        for letter in "BCDEF":
            sheet.column_dimensions[letter].width = 18
        sheet.column_dimensions["G"].width = 40

        row = 1
        sheet.cell(row=row, column=1, value="Labourmax-HR statutory verification").font = Font(
            bold=True, size=14
        )
        row += 1
        sheet.cell(
            row=row,
            column=1,
            value=(
                "THIS WORKBOOK IS A WORK AID AND NEVER A SOURCE OF TRUTH. The fixtures in "
                "reference/ are the loaded data and docs/DECISIONS.md is the register; "
                "importverification records that a figure was checked and can never change one."
            ),
        ).font = Font(italic=True)
        row += 2

        # ---------------------------------------------------- by source document
        sheet.cell(row=row, column=1, value="By source document").font = Font(bold=True, size=12)
        row += 1
        for column, title in enumerate(
            ["Source document", "Figures", "Checked", "Queried", "Outstanding"], start=1
        ):
            cell = sheet.cell(row=row, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
        header_row = row
        row += 1

        documents = {}
        for line in lines:
            documents.setdefault(line.document, []).append(line)

        first_document_row = row
        for document in sorted(documents):
            count = len(documents[document])
            sheet.cell(row=row, column=1, value=document).alignment = Alignment(wrap_text=True)
            sheet.cell(row=row, column=2, value=count)
            sheet.cell(
                row=row,
                column=3,
                value=f'=COUNTIFS(Figures!$A:$A,$A{row},Figures!$K:$K,"Y")',
            )
            sheet.cell(
                row=row,
                column=4,
                value=f'=COUNTIFS(Figures!$A:$A,$A{row},Figures!$K:$K,"QUERY")',
            )
            sheet.cell(row=row, column=5, value=f"=$B{row}-$C{row}-$D{row}")
            row += 1
        sheet.cell(row=row, column=1, value="TOTAL").font = Font(bold=True)
        sheet.cell(row=row, column=2, value=len(lines)).font = Font(bold=True)
        sheet.cell(row=row, column=3, value=f"=SUM(C{first_document_row}:C{row - 1})").font = Font(
            bold=True
        )
        sheet.cell(row=row, column=4, value=f"=SUM(D{first_document_row}:D{row - 1})").font = Font(
            bold=True
        )
        sheet.cell(row=row, column=5, value=f"=SUM(E{first_document_row}:E{row - 1})").font = Font(
            bold=True
        )
        sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
        row += 2

        # --------------------------------------------------- by reference version
        sheet.cell(row=row, column=1, value="By reference version").font = Font(bold=True, size=12)
        row += 1
        columns = ["Reference version", "Figures", "Checked", "Queried", "Verified?", "Loaded by"]
        if verifier:
            columns.append(f"{verifier} may verify?")
        for column, title in enumerate(columns, start=1):
            cell = sheet.cell(row=row, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
        row += 1

        for version in versions:
            sheet.cell(row=row, column=1, value=version["label"])
            sheet.cell(row=row, column=2, value=version["figures"])
            sheet.cell(
                row=row,
                column=3,
                value=f'=COUNTIFS(Figures!$D:$D,$A{row},Figures!$K:$K,"Y")',
            )
            sheet.cell(
                row=row,
                column=4,
                value=f'=COUNTIFS(Figures!$D:$D,$A{row},Figures!$K:$K,"QUERY")',
            )
            if version["machine_verified"]:
                state = "MACHINE ONLY — still needs a human"
            elif version["verified"]:
                state = "verified"
            elif version["superseded"]:
                state = "superseded (no verification needed)"
            else:
                state = "NOT verified"
            state_cell = sheet.cell(row=row, column=5, value=state)
            sheet.cell(row=row, column=6, value=version["loaded_by"] or "—")
            width = 6
            if verifier:
                width = 7
                blocked = version["loaded_by"].lower() == verifier.lower()
                answer = "NO — you loaded it" if blocked else "yes"
                blocked_cell = sheet.cell(row=row, column=7, value=answer)
                if blocked:
                    blocked_cell.font = Font(bold=True, color="9C0006")
            if version["machine_verified"]:
                for column in range(1, width + 1):
                    sheet.cell(row=row, column=column).fill = MACHINE_FILL
                state_cell.font = Font(bold=True)
            row += 1

        row += 1
        sheet.cell(
            row=row,
            column=1,
            value=(
                "MACHINE ONLY means verified by claude-verification@labourmax.invalid, which is "
                "a development identity and not a person. Those versions need a human pass "
                "exactly like the others — they are not done."
            ),
        ).font = Font(italic=True)

    def _figures_sheet(self, sheet, lines, machine):
        for column, (title, width) in enumerate(HEADERS, start=1):
            cell = sheet.cell(row=1, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}1"

        previous_document = None
        for index, line in enumerate(lines, start=2):
            values = [
                line.document,
                line.clause,
                line.source_url,
                line.version,
                line.table,
                line.description,
                line.value,
                line.effective_from,
                line.effective_to,
                line.key,
            ]
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=index, column=column, value=value)
                cell.alignment = Alignment(vertical="top", wrap_text=column in {1, 2, 6})
            if line.source_url:
                url_cell = sheet.cell(row=index, column=3)
                url_cell.hyperlink = line.source_url
                url_cell.style = "Hyperlink"
            # A faint band on the first line of each document, so the eye finds
            # where one gazette stops and the next starts while scrolling.
            if line.document != previous_document:
                for column in range(1, len(HEADERS) + 1):
                    sheet.cell(row=index, column=column).fill = DOCUMENT_FILL
                previous_document = line.document
            if line.version in machine:
                sheet.cell(row=index, column=4).fill = MACHINE_FILL

        last = len(lines) + 1
        if last < 2:
            return

        dropdown = DataValidation(
            type="list",
            formula1='"Y,N,QUERY"',
            allow_blank=True,
            showErrorMessage=True,
            errorTitle="Y, N or QUERY",
            error="Y checked and correct, N not yet, QUERY something is wrong — say what in Note.",
        )
        sheet.add_data_validation(dropdown)
        dropdown.add(f"K2:K{last}")

        span = f"A2:{get_column_letter(len(HEADERS))}{last}"
        sheet.conditional_formatting.add(
            span, FormulaRule(formula=['$K2="Y"'], fill=CHECKED_FILL, stopIfTrue=False)
        )
        sheet.conditional_formatting.add(
            span, FormulaRule(formula=['$K2="QUERY"'], fill=QUERY_FILL, stopIfTrue=False)
        )
