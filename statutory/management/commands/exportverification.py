"""``python manage.py exportverification`` — the workbook a person verifies from.

**The sheet people work on is CHECK GROUPS, not figures** (D-258). There are 582
loaded figures, but 126 of them are twenty-one SARS source codes at six cells
each, and all six come off one row of one table: one lookup, one question — does
what the guide says match what is loaded? Asking that six times is how somebody
stops at row 200. Grouped, the job is 129 questions.

Grouped by SOURCE DOCUMENT first, so each gazette is opened once — that is the
whole design, and sorting by anything else is the thing to fix.

**The workbook is DISPOSABLE and the database holds the record** (D-256). Every
tick already recorded is pre-filled, so re-exporting after loading new reference
rows carries the old work forward and the new figures arrive blank.

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

#: The tick sheet. Columns 10 to 14 are what ``importverification`` reads, and
#: both commands name the same positions deliberately.
CHECK_HEADERS = [
    ("Source document", 46),
    ("Clause / section", 28),
    ("Source URL", 22),
    ("Reference version", 28),
    ("Table", 22),
    ("What you are checking", 46),
    ("Every figure this tick covers", 82),
    ("Effective from", 14),
    ("Effective to", 14),
    ("Group key", 30),
    ("Checked", 11),
    ("Checked by", 24),
    ("Date checked", 14),
    ("Note", 52),
]

#: The detail sheet: every figure, one per row, and NO tick columns. Two tick
#: surfaces in one workbook is how somebody ticks the wrong one.
FIGURE_HEADERS = [
    ("Source document", 46),
    ("Clause / section", 28),
    ("Reference version", 28),
    ("Table", 22),
    ("What it is", 56),
    ("Value as loaded", 22),
    ("Effective from", 14),
    ("Effective to", 14),
    ("Row key", 30),
    ("Group key", 30),
]

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
CHECKED_FILL = PatternFill("solid", fgColor="D5EAD3")
QUERY_FILL = PatternFill("solid", fgColor="FBE2C7")
MACHINE_FILL = PatternFill("solid", fgColor="DCE6F5")
DOCUMENT_FILL = PatternFill("solid", fgColor="EDEDED")

#: The workbook's own words for an outcome. ``importverification`` reads them
#: back through IMPORTED_AS, which is the inverse of this.
CHECKED_TEXT = {"checked": "Y", "queried": "QUERY"}


class Command(BaseCommand):
    help = "Export the reference data to a verification workbook, grouped for checking."

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
            help="Overwrite an existing file. Rarely needed now that ticks are "
            "recorded in the database and pre-filled on every export: what --force "
            "still destroys is ticks entered but never imported.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if path.exists() and not options["force"]:
            raise CommandError(
                f"{path} already exists. Every tick that has been IMPORTED is safe — it "
                f"is in the database and this export would pre-fill it again. What "
                f"would be lost is anything ticked in that file and not yet imported. "
                f"Run importverification on it first, or pass --force, or export to a "
                f"new filename."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = verification.lines()
        groups = verification.check_groups(lines)
        versions = verification.version_state()
        checks = verification.latest_checks()
        machine = {v["label"] for v in versions if v["machine_verified"]}

        book = Workbook()
        self._summary_sheet(
            book.active, lines, groups, versions, checks, options["verifier"].strip()
        )
        self._check_sheet(book.create_sheet("Checks"), groups, machine, checks)
        self._figures_sheet(book.create_sheet("Figures"), lines, groups)
        book.save(path)

        done = sum(1 for line in lines if line.key in checks)
        self.stdout.write(self.style.SUCCESS(f"Wrote {path}"))
        self.stdout.write(
            f"  {len(groups)} check groups over {len(lines)} figures, "
            f"{len({line.document for line in lines})} source documents"
        )
        self.stdout.write(f"  {len(versions)} reference versions, {len(machine)} machine-verified")
        self.stdout.write(f"  {done} figure(s) already checked, carried forward")

    # ------------------------------------------------------------------ sheets

    def _summary_sheet(self, sheet, lines, groups, versions, checks, verifier):
        sheet.title = "Summary"
        sheet.column_dimensions["A"].width = 62
        for letter in "BCDEFG":
            sheet.column_dimensions[letter].width = 17

        row = 1
        sheet.cell(row=row, column=1, value="Labourmax-HR statutory verification").font = Font(
            bold=True, size=14
        )
        row += 1
        sheet.cell(
            row=row,
            column=1,
            value=(
                "Work on the CHECKS sheet. One row there is one lookup: open the document, "
                "read the clause, and confirm every figure listed on the row. FIGURES is "
                "detail only and has nothing to tick."
            ),
        ).font = Font(bold=True)
        row += 1
        sheet.cell(
            row=row,
            column=1,
            value=(
                "THIS WORKBOOK IS A WORK AID AND NEVER A SOURCE OF TRUTH. The fixtures in "
                "reference/ are the loaded data and docs/DECISIONS.md is the register; "
                "importverification records that a figure was checked and can never change "
                "one. The file itself is DISPOSABLE — every imported tick is in the database "
                "and is pre-filled on the next export. The counts below are as at export."
            ),
        ).font = Font(italic=True)
        row += 2

        # ---------------------------------------------------- by source document
        sheet.cell(row=row, column=1, value="By source document").font = Font(bold=True, size=12)
        row += 1
        for column, title in enumerate(
            ["Source document", "Checks", "Figures", "Done", "Queried", "Outstanding"], start=1
        ):
            cell = sheet.cell(row=row, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
        header_row = row
        row += 1

        # Counted from the DATABASE, not with a COUNTIFS over another sheet:
        # openpyxl writes a formula and only Excel evaluates it, so a freshly
        # written file would read as zero everywhere until somebody opened it.
        by_document: dict[str, list] = {}
        for group in groups:
            by_document.setdefault(group.document, []).append(group)

        for document in sorted(by_document):
            members = by_document[document]
            done = sum(1 for g in members if _state(checks, g) == "checked")
            queried = sum(1 for g in members if _state(checks, g) == "queried")
            sheet.cell(row=row, column=1, value=document).alignment = Alignment(wrap_text=True)
            sheet.cell(row=row, column=2, value=len(members))
            sheet.cell(row=row, column=3, value=sum(len(g.figures) for g in members))
            sheet.cell(row=row, column=4, value=done)
            sheet.cell(row=row, column=5, value=queried)
            sheet.cell(row=row, column=6, value=len(members) - done - queried)
            row += 1

        sheet.cell(row=row, column=1, value="TOTAL").font = Font(bold=True)
        totals = (
            len(groups),
            len(lines),
            sum(1 for g in groups if _state(checks, g) == "checked"),
            sum(1 for g in groups if _state(checks, g) == "queried"),
            sum(1 for g in groups if _state(checks, g) == "open"),
        )
        for column, total in enumerate(totals, start=2):
            sheet.cell(row=row, column=column, value=total).font = Font(bold=True)
        sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
        row += 2

        # --------------------------------------------------- by reference version
        sheet.cell(row=row, column=1, value="By reference version").font = Font(bold=True, size=12)
        row += 1
        columns = ["Reference version", "Checks", "Done", "Queried", "Verified?", "Loaded by"]
        if verifier:
            columns.append(f"{verifier} may verify?")
        for column, title in enumerate(columns, start=1):
            cell = sheet.cell(row=row, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
        row += 1

        by_version: dict[str, list] = {}
        for group in groups:
            by_version.setdefault(group.version, []).append(group)

        for version in versions:
            members = by_version.get(version["label"], [])
            sheet.cell(row=row, column=1, value=version["label"])
            sheet.cell(row=row, column=2, value=len(members))
            sheet.cell(
                row=row, column=3, value=sum(1 for g in members if _state(checks, g) == "checked")
            )
            sheet.cell(
                row=row, column=4, value=sum(1 for g in members if _state(checks, g) == "queried")
            )
            state_cell = sheet.cell(row=row, column=5, value=_version_text(version))
            sheet.cell(row=row, column=6, value=version["loaded_by"] or "—")
            width = 6
            if verifier:
                width = 7
                blocked = version["loaded_by"].lower() == verifier.lower()
                blocked_cell = sheet.cell(
                    row=row, column=7, value="NO — you loaded it" if blocked else "yes"
                )
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
                "MACHINE ONLY means verified by claude-verification@labourmax.invalid, which "
                "is a development identity and not a person. Those versions need a human pass "
                "exactly like the others — they are not done."
            ),
        ).font = Font(italic=True)

    def _check_sheet(self, sheet, groups, machine, checks):
        for column, (title, width) in enumerate(CHECK_HEADERS, start=1):
            cell = sheet.cell(row=1, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(CHECK_HEADERS))}1"

        previous_document = None
        for index, group in enumerate(groups, start=2):
            values = [
                group.document,
                group.clause,
                group.source_url,
                group.version,
                group.table,
                group.label,
                group.summary,
                group.effective_from,
                group.effective_to,
                group.key,
            ]
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=index, column=column, value=value)
                cell.alignment = Alignment(vertical="top", wrap_text=column in {1, 2, 6, 7})
            if group.source_url:
                url_cell = sheet.cell(row=index, column=3)
                url_cell.hyperlink = group.source_url
                url_cell.style = "Hyperlink"
            if group.document != previous_document:
                for column in range(1, len(CHECK_HEADERS) + 1):
                    sheet.cell(row=index, column=column).fill = DOCUMENT_FILL
                previous_document = group.document
            if group.version in machine:
                sheet.cell(row=index, column=4).fill = MACHINE_FILL

            # Carry forward what is already recorded (D-256). A group is only
            # pre-filled where EVERY figure in it agrees; a part-recorded group
            # stays open, because a tick has to mean all of them.
            state = _state(checks, group)
            if state in {"checked", "queried"}:
                first = checks[group.figures[0].key]
                sheet.cell(row=index, column=11, value=CHECKED_TEXT[state])
                sheet.cell(row=index, column=12, value=first.checked_by.email)
                sheet.cell(row=index, column=13, value=f"{first.checked_on:%Y-%m-%d}")
                sheet.cell(row=index, column=14, value=first.note)

        last = len(groups) + 1
        if last < 2:
            return

        dropdown = DataValidation(
            type="list",
            formula1='"Y,N,QUERY"',
            allow_blank=True,
            showErrorMessage=True,
            errorTitle="Y, N or QUERY",
            error=(
                "Y: every figure on this row matches the document. N: not yet. "
                "QUERY: one or more is wrong - say which in Note."
            ),
        )
        sheet.add_data_validation(dropdown)
        dropdown.add(f"K2:K{last}")

        span = f"A2:{get_column_letter(len(CHECK_HEADERS))}{last}"
        sheet.conditional_formatting.add(
            span, FormulaRule(formula=['$K2="Y"'], fill=CHECKED_FILL, stopIfTrue=False)
        )
        sheet.conditional_formatting.add(
            span, FormulaRule(formula=['$K2="QUERY"'], fill=QUERY_FILL, stopIfTrue=False)
        )

    def _figures_sheet(self, sheet, lines, groups):
        group_of = {key: group.key for group in groups for key in group.row_keys}
        for column, (title, width) in enumerate(FIGURE_HEADERS, start=1):
            cell = sheet.cell(row=1, column=column, value=title)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.column_dimensions[get_column_letter(column)].width = width
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(FIGURE_HEADERS))}1"

        for index, line in enumerate(lines, start=2):
            values = [
                line.document,
                line.clause,
                line.version,
                line.table,
                line.description,
                line.value,
                line.effective_from,
                line.effective_to,
                line.key,
                group_of.get(line.key, ""),
            ]
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row=index, column=column, value=value)
                cell.alignment = Alignment(vertical="top", wrap_text=column in {1, 2, 5})


def _state(checks, group) -> str:
    """A group is checked only where every figure in it is, queried if any is."""
    states = [checks.get(key) for key in group.row_keys]
    if any(state is None for state in states):
        return "open"
    if any(state.outcome == "queried" for state in states):
        return "queried"
    return "checked"


def _version_text(version) -> str:
    if version["machine_verified"]:
        return "MACHINE ONLY — still needs a human"
    if version["verified"]:
        return "verified"
    if version["superseded"]:
        return "superseded (no verification needed)"
    return "NOT verified"
