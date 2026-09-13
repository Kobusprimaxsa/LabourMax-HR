"""Build the statutory verification workbook from the loaded reference data.

    python manage.py shell -c "exec(open('tools/build_verification_workbook.py').read())"

or simply::

    python tools/build_verification_workbook.py

**Why this is a script and not a one-off.** The first version of this workbook was
built by hand, and it went stale the same day — two new statutory parameters were
loaded a few hours later and the workbook silently did not know about them. A
verification document that lags the data is worse than none, because the rows it
omits are the ones nobody checks. It now reads the database, so regenerating after
any load is one command and the count of rows is the count of rows.

**It reads the database, not the fixture files.** The question being verified is "is
what the system will actually use correct", and that is the loaded row. A fixture that
was never loaded, or loaded into a different database, is not what a payroll run reads.

Every sheet carries the same three empty columns — ``Agrees?``, ``Correct value``,
``Where you checked`` — because a verification pass that records only "yes" is not
evidence. The disagreement and its source are the valuable half.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sys

import django
from django.apps import apps as django_apps

if not django_apps.ready:  # pragma: no cover - convenience for direct execution
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "labourmax.settings.dev")
    django.setup()

from openpyxl import Workbook  # noqa: E402
from openpyxl.formatting.rule import CellIsRule  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

from statutory.models import (  # noqa: E402
    Bank,
    LeaveRuleSet,
    MedicalTaxCreditRate,
    MinimumWageRate,
    PayeRebate,
    PayeTaxBracket,
    PublicHoliday,
    ReferenceDataVersion,
    SarsSourceCode,
    StatutoryParameter,
    StatutoryWatchItem,
    TaxYear,
    TerminationRuleSet,
    WorkingTimeRuleSet,
)

OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "reference"
    / "verification"
    / "Labourmax_statutory_verification.xlsx"
)

FONT = "Arial"
HEADER_FILL = PatternFill("solid", start_color="1F3864")
FLAG_FILL = PatternFill("solid", start_color="FCE4D6")
RULE_FILL = PatternFill("solid", start_color="E2EFDA")
INPUT_FILL = PatternFill("solid", start_color="FFF2CC")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

#: The three columns Kobus fills in. Named once so the legend, the colouring and the
#: progress formulas cannot drift apart.
ANSWER_HEADERS = ["Agrees?", "Correct value", "Where you checked"]

#: A row whose notes contain this is on the Priority sheet. Chosen at load time by
#: whoever researched the figure, not guessed here.
FLAG_WORD = "VERIFY"

#: The only three answers the Agrees? column accepts. A free-text column collects
#: "yes", "Y", "ok", "checked" and a tick character from the same person over three
#: sittings, and the progress formulas then count strings rather than decisions.
#: "Query" is here because the honest third answer to "does this agree" is often
#: "I could not find the document", and a person with no way to say that writes Y.
ANSWER_OPTIONS = ["Y", "N", "Query"]

AGREE_FILL = PatternFill("solid", start_color="C6EFCE")
DISAGREE_FILL = PatternFill("solid", start_color="FFC7CE")
QUERY_FILL = PatternFill("solid", start_color="FFEB9C")


#: How many leading columns identify a row for the purposes of carrying a previous
#: answer forward. Deliberately includes the loaded VALUE where there is one: if the
#: figure changed, the row is a different question and the old answer must not follow
#: it. It excludes the free-text citation and note columns, which get reworded without
#: the figure changing.
KEY_COLUMNS = {
    "Priority": 4,
    "Rates and parameters": 4,
    "Rule sets": 4,
    "SARS source codes": 4,
    "Public holidays": 2,
    "Banks": 2,
}


def _is_flagged(notes: str) -> bool:
    return FLAG_WORD in (notes or "")


def _row_key(sheet_title, row):
    width = KEY_COLUMNS.get(sheet_title, 4)
    return tuple(str(value or "").strip() for value in row[:width])


def _shared_key(sheet_title, row):
    """An identity that survives a row appearing on TWO sheets.

    The Priority sheet repeats rows that also live on their home sheet, so a bank or a
    source code gets asked twice. Answering it once should be enough — this is what
    lets an answer on either sheet satisfy both.
    """
    values = [str(value or "").strip() for value in row]
    if sheet_title == "Priority":
        if values[0] == "bank":
            return ("bank", values[1], values[3])
        if values[0].isdigit():
            return ("sars", values[0])
        return None
    if sheet_title == "Banks":
        return ("bank", values[0], values[1])
    if sheet_title == "SARS source codes":
        return ("sars", values[0])
    return None


def read_previous_answers(path):
    """Every answer already recorded in an earlier copy of this workbook.

    Returns two maps: one keyed per sheet, one keyed across sheets for the rows the
    Priority sheet duplicates.

    **Regenerating without this discards the work**, which is the whole reason it
    exists: the first regeneration after a verification session would have handed back
    a blank workbook and asked for fifty-nine answers a second time.
    """
    from openpyxl import load_workbook

    per_sheet, shared = {}, {}
    if not path or not pathlib.Path(path).exists():
        return per_sheet, shared

    book = load_workbook(path, data_only=True)
    for title in book.sheetnames:
        if title not in KEY_COLUMNS:
            continue
        ws = book[title]
        answer_start = ws.max_column - len(ANSWER_HEADERS) + 1
        for excel_row in range(5, ws.max_row + 1):
            values = [ws.cell(row=excel_row, column=c).value for c in range(1, ws.max_column + 1)]
            answers = values[answer_start - 1 :]
            if not any(a not in (None, "") for a in answers):
                continue
            per_sheet[(title, _row_key(title, values))] = answers
            shared_key = _shared_key(title, values)
            if shared_key:
                shared[shared_key] = answers
    return per_sheet, shared


def _sheet(book, title, heading, blurb, headers):
    ws = book.create_sheet(title)
    ws["A1"] = heading
    ws["A1"].font = Font(name=FONT, size=14, bold=True, color="1F3864")
    ws["A2"] = blurb
    ws["A2"].font = Font(name=FONT, size=10, italic=True, color="595959")
    ws["A2"].alignment = Alignment(wrap_text=False)

    for index, header in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=index, value=header)
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A5"
    return ws


def _write(ws, rows, *, flag_column=None, base_fill=None, previous=(None, None)):
    """Write data rows, tinting the flagged ones and the three answer columns.

    ``previous`` carries any answers recorded in an earlier copy, matched on the row's
    identity columns. An answer follows its row; it does not follow its position.
    """
    per_sheet, shared = previous
    answer_start = ws.max_column - len(ANSWER_HEADERS) + 1
    for offset, row in enumerate(rows):
        excel_row = 5 + offset
        flagged = flag_column is not None and _is_flagged(str(row[flag_column] or ""))

        if per_sheet is not None:
            carried = per_sheet.get((ws.title, _row_key(ws.title, row)))
            if carried is None and shared:
                shared_key = _shared_key(ws.title, row)
                carried = shared.get(shared_key) if shared_key else None
            if carried:
                row = list(row)
                for index, value in enumerate(carried):
                    if answer_start - 1 + index < len(row):
                        row[answer_start - 1 + index] = value

        for index, value in enumerate(row, start=1):
            cell = ws.cell(row=excel_row, column=index, value=value)
            cell.font = Font(name=FONT, size=10)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=index >= 5)
            if index >= answer_start:
                cell.fill = INPUT_FILL
            elif flagged:
                cell.fill = FLAG_FILL
            elif base_fill is not None:
                cell.fill = base_fill
    return 5 + len(rows) - 1 if rows else 4


def _add_answer_validation(ws, last_row):
    """A dropdown on Agrees?, and colour on the answer.

    The dropdown is the point: it makes the three answers the only three answers, so
    the progress counters on the front sheet count decisions rather than whatever
    somebody typed that afternoon. The colour is so a filled sheet can be read at a
    glance for the rows that did NOT agree, which are the ones worth returning to.
    """
    if last_row < 5:
        return
    column = get_column_letter(ws.max_column - 2)
    span = f"{column}5:{column}{last_row}"

    validation = DataValidation(
        type="list",
        formula1=f'"{",".join(ANSWER_OPTIONS)}"',
        allow_blank=True,
        showDropDown=False,
        errorTitle="One of three answers",
        error="Y if the loaded figure matches the source, N if it does not, "
        "Query if you could not find the document.",
        promptTitle="Does the loaded value agree?",
        prompt="Y, N or Query.",
        showInputMessage=True,
        showErrorMessage=True,
    )
    ws.add_data_validation(validation)
    validation.add(span)

    for option, fill in (
        ("Y", AGREE_FILL),
        ("N", DISAGREE_FILL),
        ("Query", QUERY_FILL),
    ):
        ws.conditional_formatting.add(
            span,
            CellIsRule(operator="equal", formula=[f'"{option}"'], fill=fill),
        )


def _widths(ws, widths):
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width


def _scope(rate: MinimumWageRate) -> str:
    parts = [rate.sector.code if rate.sector else "NMW"]
    if rate.sector_area:
        parts.append(rate.sector_area.code)
    if rate.job_grade:
        parts.append(rate.job_grade.code)
    if rate.hours_band != MinimumWageRate.HoursBand.ALL:
        parts.append(rate.hours_band)
    return " / ".join(parts)


def _rule_rows(model, label, applies_to_field="sector"):
    """Every column of every rule set row, one spreadsheet line each.

    Field by field rather than row by row, because a rule set is thirty numbers and a
    person verifying it works down a gazette one clause at a time.
    """
    skip = {
        "id",
        "created_at",
        "updated_at",
        "created_by_user",
        "updated_by_user",
        "sector",
        "source_reference",
        "source_url",
        "notes",
        "effective_from",
        "effective_to",
    }
    rows = []
    for instance in model.objects.select_related(applies_to_field).all():
        applies = getattr(instance, applies_to_field)
        applies_label = applies.code if applies else "BCEA default"
        for field in instance._meta.fields:
            if field.name in skip:
                continue
            value = getattr(instance, field.name)
            rows.append(
                [
                    label,
                    applies_label,
                    field.name,
                    "" if value is None else str(value),
                    instance.source_reference,
                    instance.notes or "",
                    "",
                    "",
                    "",
                ]
            )
    return rows


def build(merge_from=None):
    previous = read_previous_answers(merge_from)
    book = Workbook()
    book.remove(book.active)

    priority_rows = []

    # ------------------------------------------------- rates and parameters
    rate_rows = []
    for rate in MinimumWageRate.objects.select_related("sector", "sector_area", "job_grade").all():
        rate_rows.append(
            [
                "minimum_wage_rate",
                _scope(rate),
                "hourly_rate",
                f"{rate.hourly_rate}",
                rate.source_reference,
                rate.source_url or "",
                "",
                "",
                "",
            ]
        )

    # tax_year is not a cited table - it carries no source_reference, because a tax
    # year's dates are the definition of the year rather than a figure somebody
    # gazetted. Listed anyway, so the span every bracket hangs off is on the page.
    for year in TaxYear.objects.all():
        rate_rows.append(
            [
                "tax_year",
                year.label,
                "start and end",
                f"{year.start_date} to {year.end_date}",
                "Income Tax Act — the year of assessment",
                "",
                "",
                "",
                "",
            ]
        )

    for bracket in PayeTaxBracket.objects.select_related("tax_year").all():
        upper = bracket.income_to if bracket.income_to is not None else "and above"
        rate_rows.append(
            [
                "paye_tax_bracket",
                f"{bracket.tax_year.label} band {bracket.bracket_order}",
                "base + marginal rate",
                f"{bracket.income_from} to {upper}: {bracket.base_tax} + "
                f"{bracket.marginal_rate_pct}%",
                bracket.source_reference,
                bracket.source_url or "",
                "",
                "",
                "",
            ]
        )

    for rebate in PayeRebate.objects.select_related("tax_year").all():
        rate_rows.append(
            [
                "paye_rebate",
                f"{rebate.tax_year.label} {rebate.rebate_type}",
                "amount / threshold",
                f"{rebate.annual_amount} / {rebate.tax_threshold_annual}",
                rebate.source_reference,
                rebate.source_url or "",
                "",
                "",
                "",
            ]
        )

    for credit in MedicalTaxCreditRate.objects.select_related("tax_year").all():
        rate_rows.append(
            [
                "medical_tax_credit_rate",
                credit.tax_year.label,
                "main / first / additional",
                f"{credit.main_member_monthly} / {credit.first_dependant_monthly} / "
                f"{credit.additional_dependant_monthly}",
                credit.source_reference,
                credit.source_url or "",
                "",
                "",
                "",
            ]
        )

    for parameter in StatutoryParameter.objects.all():
        value = (
            f"{parameter.value_numeric} {parameter.unit}"
            if parameter.value_numeric is not None
            else parameter.value_text
        )
        row = [
            "statutory_parameter",
            parameter.parameter_code,
            f"from {parameter.effective_from}",
            value,
            parameter.source_reference,
            parameter.source_url or "",
            "",
            "",
            "",
        ]
        rate_rows.append(row)
        if _is_flagged(parameter.notes):
            priority_rows.append(row[:5] + [parameter.notes, "", "", ""])

    ws = _sheet(
        book,
        "Rates and parameters",
        "Wage rates, PAYE tables and scalar parameters",
        "The money figures. Every one is either gazetted or published by SARS, so every "
        "one has a document you can open.",
        ["Table", "Scope", "Field", "Loaded value", "Cited to", "Source link", *ANSWER_HEADERS],
    )
    last = _write(ws, rate_rows, previous=previous)
    _add_answer_validation(ws, last)
    _widths(ws, [24, 26, 22, 34, 52, 40, 12, 18, 26])

    # ----------------------------------------------------------- rule sets
    rule_rows = (
        _rule_rows(LeaveRuleSet, "leave_rule_set")
        + _rule_rows(WorkingTimeRuleSet, "working_time_rule_set")
        + _rule_rows(TerminationRuleSet, "termination_rule_set")
    )
    # ONE priority row per flagged rule set, not one per field. A rule set's notes
    # column is a single compound note covering the whole row — the domestic working
    # time note runs to a paragraph and its VERIFY applies to the standby allowance
    # alone. Copying it onto all thirty fields put thirty rows on the Priority sheet
    # and buried the two figures that genuinely rest on a weak source.
    seen_rule_sets = set()
    for row in rule_rows:
        key = (row[0], row[1])
        if _is_flagged(row[5]) and key not in seen_rule_sets:
            seen_rule_sets.add(key)
            priority_rows.append(
                [
                    row[0],
                    row[1],
                    "see the note — it names the field",
                    "",
                    row[4],
                    row[5],
                    "",
                    "",
                    "",
                ]
            )

    ws = _sheet(
        book,
        "Rule sets",
        "Leave, working time and termination rules",
        "One line per column per rule set: the BCEA default, domestic (SD7) and contract "
        "cleaning (SD1). A zero can mean the statute sets no figure — read the note.",
        [
            "Rule set",
            "Applies to",
            "Field",
            "Loaded value",
            "Cited to",
            "Note",
            *ANSWER_HEADERS,
        ],
    )
    last = _write(ws, rule_rows, flag_column=5, base_fill=RULE_FILL, previous=previous)
    _add_answer_validation(ws, last)
    _widths(ws, [24, 20, 40, 16, 46, 60, 12, 18, 26])

    # --------------------------------------------------------- source codes
    code_rows = []
    for code in SarsSourceCode.objects.all():
        row = [
            code.code,
            code.description,
            code.code_group,
            f"PAYE {code.is_taxable} / UIF {code.is_uif_remuneration} / "
            f"SDL {code.is_sdl_remuneration} / COIDA {code.is_coida_remuneration}",
            code.source_reference,
            code.notes or "",
            "",
            "",
            "",
        ]
        code_rows.append(row)
        if _is_flagged(code.notes):
            priority_rows.append(row)

    ws = _sheet(
        book,
        "SARS source codes",
        "Source codes and their four base flags",
        "The code and its wording are SARS's. The four flags are a reading of three "
        "statutes with three definitions of remuneration — those are what to check. "
        "READ THE COLUMN HEADING CAREFULLY: a flag says whether this amount is part of "
        "the BASE the levy is computed ON, not whether the levy is deducted. Code 4102 "
        "is PAYE itself and its PAYE flag is False, because PAYE is not calculated on "
        "PAYE. Every deduction and total reads that way.",
        [
            "Code",
            "Description",
            "Group",
            "Is this amount IN the base for…",
            "Cited to",
            "Reasoning",
            *ANSWER_HEADERS,
        ],
    )
    last = _write(ws, code_rows, flag_column=5, previous=previous)
    _add_answer_validation(ws, last)
    _widths(ws, [10, 40, 20, 46, 46, 60, 12, 18, 26])

    # ------------------------------------------------------ public holidays
    holiday_rows = [
        [
            str(holiday.holiday_date),
            holiday.name,
            f"shifted from {holiday.shifted_from_date}" if holiday.shifted_from_date else "",
            holiday.source_reference,
            "",
            "",
            "",
        ]
        for holiday in PublicHoliday.objects.all()
    ]
    ws = _sheet(
        book,
        "Public holidays",
        "Public holidays, 2026 and 2027",
        "Generated from the Public Holidays Act, including the Sunday rule: a holiday "
        "falling on a Sunday is observed on the Monday.",
        ["Date", "Holiday", "Monday shift", "Cited to", *ANSWER_HEADERS],
    )
    last = _write(ws, holiday_rows, previous=previous)
    _add_answer_validation(ws, last)
    _widths(ws, [14, 34, 14, 46, 12, 18, 26])

    # ----------------------------------------------------------------- banks
    bank_rows = []
    for bank in Bank.objects.all():
        row = [
            bank.name,
            bank.universal_branch_code,
            bank.notes or "",
            "",
            "",
            "",
        ]
        bank_rows.append(row)
        if _is_flagged(bank.notes):
            priority_rows.append(
                [
                    "bank",
                    bank.name,
                    "universal_branch_code",
                    bank.universal_branch_code,
                    "",
                    bank.notes,
                    "",
                    "",
                    "",
                ]
            )

    ws = _sheet(
        book,
        "Banks",
        "Banks and universal branch codes",
        "No account number lengths are loaded — NULL means nobody has confirmed that "
        "bank's rule, and a guessed bound leaves somebody unpaid.",
        ["Bank", "Universal branch code", "Note", *ANSWER_HEADERS],
    )
    last = _write(ws, bank_rows, flag_column=2, previous=previous)
    _add_answer_validation(ws, last)
    _widths(ws, [40, 22, 60, 12, 18, 26])

    # -------------------------------------------------------- watch calendar
    watch_rows = [
        [
            item.name,
            item.typical_publication_window or "",
            f"{item.source_name} {item.source_url}".strip(),
            item.description or "",
        ]
        for item in StatutoryWatchItem.objects.all()
    ]
    ws = _sheet(
        book,
        "Watch calendar",
        "What moves, and when to look for it",
        "Loaded as data so a new person inherits the calendar rather than the folklore.",
        ["What", "Expected", "Where to look", "Note"],
    )
    _write(ws, watch_rows)
    _widths(ws, [40, 18, 40, 60])

    # ------------------------------------------------------------- versions
    version_rows = [
        [
            version.version_label,
            str(version.applies_from),
            version.checksum[:12] if version.checksum else "",
            "VERIFIED" if version.verified_at else "not verified",
            (version.description or "")[:200],
        ]
        for version in ReferenceDataVersion.objects.all()
    ]
    ws = _sheet(
        book,
        "Versions loaded",
        f"The {len(version_rows)} reference data versions",
        "A version that is not verified is not in force, and every payroll run stays "
        "blocked. That is deliberate.",
        ["Version", "Applies from", "Checksum", "State", "What it holds"],
    )
    _write(ws, version_rows)
    _widths(ws, [34, 14, 16, 14, 80])

    # ------------------------------------------------------------- priority
    ws = _sheet(
        book,
        "Priority",
        "Priority — check these first",
        "Every row whose own note says VERIFY. Secondary sources, single sources, and "
        "figures whose gazette could not be pinned down. Start here.",
        ["Table", "Scope", "Field", "Loaded value", "Cited to", "Why it is here", *ANSWER_HEADERS],
    )
    last = _write(ws, priority_rows, base_fill=FLAG_FILL, previous=previous)
    _add_answer_validation(ws, last)
    _widths(ws, [24, 26, 30, 30, 46, 64, 12, 18, 26])
    book.move_sheet("Priority", offset=-(len(book.sheetnames) - 1))

    # ---------------------------------------------------------- how to use
    ws = book.create_sheet("How to use this", 0)
    ws["A1"] = "Labourmax-HR — statutory data verification"
    ws["A1"].font = Font(name=FONT, size=16, bold=True, color="1F3864")

    lines = [
        "",
        f"Generated {datetime.date.today():%d %B %Y} from the loaded reference data. "
        f"Regenerate with: python tools/build_verification_workbook.py",
        "",
        "Every figure the system will use is listed here with the source it was taken "
        "from. Nothing is verified until you say so, and nothing unverified is in force — "
        "every payroll run is blocked until it is. That is deliberate.",
        "",
        "HOW TO WORK THROUGH IT",
        "",
        "1. Start on the Priority sheet. Those rows rest on secondary or single sources, "
        "or on a gazette nobody could pin down. They are the ones most likely to be wrong.",
        "2. For each row, open the cited document and compare. The Source link column "
        "carries the URL where there is one.",
        "3. Fill in the three shaded columns. 'Agrees?' is a dropdown with three "
        "options and will not accept anything else:",
        "",
        "       Y       the loaded value matches the source document",
        "       N       it does not — put the right figure in 'Correct value'",
        "       Query   you could not find or could not read the document",
        "",
        "   Y turns the cell green, N red, Query amber, so a part-finished sheet can be "
        "read at a glance for the rows worth returning to.",
        "",
        "   'Query' exists because that is the honest third answer, and a person with "
        "no way to say it writes Y. A figure nobody could check is not a figure that "
        "agrees.",
        "",
        "   Example of a filled row:",
        "       Agrees?  N   |   Correct value  R672,000   |   Where you checked  "
        "GN 4711, GG 51234, 30 May 2026, p.4",
        "",
        "4. A disagreement is the valuable outcome, not a failure. Tell Claude and the "
        "fixture gets a corrected row with a new citation — the loader refuses to edit "
        "a loaded value in place, which is the whole point of it.",
        "",
        "WHEN YOU ARE DONE",
        "",
        "Run, once per version, naming yourself:",
        "",
        "    python manage.py verifystatutory REF-2026.03.01 --verified-by you@example.com "
        "--current-through 2027-02-28 --allow-self-verification",
        "",
        "The --allow-self-verification flag exists because verification is meant to be a "
        "second pair of eyes and you are a one-person team. It records that fact on the "
        "version rather than pretending otherwise. The real second pass is the labour law "
        "reviewer (open decision O-06).",
        "",
        "--golden-tests-passed is deliberately NOT included above. The golden tests "
        "reproduce published SARS worked examples and do not exist yet, so a version "
        "verified today is verified-but-not-usable until they do. That is the system "
        "refusing to take your word for the arithmetic, which is correct.",
    ]
    for offset, line in enumerate(lines, start=2):
        cell = ws.cell(row=offset, column=1, value=line)
        cell.font = Font(
            name=FONT,
            size=11,
            bold=line.isupper() and bool(line.strip()),
            color="1F3864" if line.isupper() and line.strip() else "000000",
        )
    ws.column_dimensions["A"].width = 110

    # Progress, as live formulas rather than a number that is wrong by tomorrow.
    ws["C2"] = "Progress"
    ws["C2"].font = Font(name=FONT, size=12, bold=True, color="1F3864")
    progress = [
        ("Priority", "Priority"),
        ("Rates and parameters", "Rates and parameters"),
        ("Rule sets", "Rule sets"),
        ("SARS source codes", "SARS source codes"),
        ("Public holidays", "Public holidays"),
        ("Banks", "Banks"),
    ]
    for offset, (label, sheet_name) in enumerate(progress, start=3):
        answer_column = get_column_letter(book[sheet_name].max_column - 2)
        ws.cell(row=offset, column=3, value=label).font = Font(name=FONT, size=10)
        target = f"'{sheet_name}'!{answer_column}5:{answer_column}2000"
        formula = f'=COUNTA({target})&" of "&COUNTA(\'{sheet_name}\'!A5:A2000)&" checked"'
        cell = ws.cell(row=offset, column=4, value=formula)
        cell.font = Font(name=FONT, size=10, bold=True)
        cell.fill = INPUT_FILL
    ws.column_dimensions["C"].width = 26
    ws.column_dimensions["D"].width = 22

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    book.save(OUTPUT)

    carried = 0
    for title in KEY_COLUMNS:
        ws = book[title]
        column = ws.max_column - 2
        carried += sum(
            1
            for r in range(5, ws.max_row + 1)
            if ws.cell(row=r, column=column).value not in (None, "")
        )

    return (
        OUTPUT,
        len(priority_rows),
        sum(book[name].max_row - 4 for name in book.sheetnames if name != "How to use this"),
        carried,
    )


if __name__ == "__main__":
    # An existing workbook at the output path is merged from by default, so
    # regenerating after a load never asks for an answer twice. Pass a path to merge
    # from somewhere else — a copy that was filled in outside the repo, say.
    merge_from = sys.argv[1] if len(sys.argv) > 1 else (OUTPUT if OUTPUT.exists() else None)
    path, priority, total, carried = build(merge_from)
    print(f"Wrote {path}")
    print(f"  {total} rows to check, {priority} of them on the Priority sheet.")
    if merge_from:
        print(f"  Carried {carried} existing answers forward from {merge_from}.")
