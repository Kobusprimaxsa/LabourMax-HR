"""Bulk import mechanics — shared by every app that lets an employer upload a
spreadsheet instead of capturing rows one at a time.

D-155. Extracted out of ``employees/importing.py`` (D-122) once a second
importer — the attendance import, P5 chunk 3 — needed the exact same
machinery: preview is apply, rolled back; a batch is a unit, both ways; the
status machine; the report shape; the source file purge once a batch reaches
a terminal status. Two independent copies of those invariants would diverge
silently, and the divergence would be the kind of bug nobody notices until a
reconciliation six months later.

**What lives here** (true regardless of what a row means): the column-spec
dataclass and its generic cell parsing, ``RowIssue``/``BatchResult``'s shape,
the status machine (``ALLOWED_TRANSITIONS``/``transition()``), the
savepoint-based orchestration skeleton for preview/apply/reverse, the
template-workbook builder, and ``purge_source_file``.

**What does NOT live here, deliberately**: what a row means, what validating
it costs, and what "apply" actually writes or "reverse" actually undoes. Those
differ enough between an employee upload (only ever creates) and an
attendance upload (routinely replaces) that forcing them into one shared
function would hide the difference rather than express it. Each app supplies
its own ``run_rows`` callable (turns parsed rows into a ``BatchResult``), and
its own ``mutate`` callable for reverse (what "undo" means for that table).
``apply_batch`` takes two further, optional hooks for the two places a
domain's rules still need a say inside the shared shell: ``extra_refusal``
(a second reason, beyond a blocking row, that the whole batch should not
write — employees' below-minimum-wage-without-acknowledgement), and
``on_success`` (extra fields to persist on the batch once it commits —
attendance's prior-state snapshot, used only by reverse).

There is no ``validate_only`` flag anywhere in this module, on purpose: it
would be a second, divergent validation path by another name.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.files import purge_content
from core.managers import tenant_context_of

# --------------------------------------------------------------- status machine

UPLOADED = "uploaded"
VALIDATING = "validating"
PREVIEW = "preview"
APPLIED = "applied"
REVERSED = "reversed"
FAILED = "failed"


class ImportRefusedError(Exception):
    """The batch may not be applied or reversed as asked. Nothing was written."""


class IllegalTransitionError(Exception):
    """The batch cannot move to that status from where it is."""


#: What each status may legally become. Enforced here, in the service layer —
#: the CHECK constraint on the column only proves the value is a known one,
#: never that the move from the row's previous value was legal. Plain strings
#: rather than a model's own ``Status`` class, so this one dict serves every
#: importer's batch model: a ``TextChoices`` member compares equal to its
#: string value, so either can be passed to ``transition()``.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # APPLIED is reachable directly from UPLOADED (and from PREVIEW, and from a
    # retry after FAILED): apply_batch() re-validates every row itself rather
    # than trusting an earlier preview's report, so a prior preview call is a
    # convenience for the employer, never a precondition the state machine
    # enforces.
    UPLOADED: frozenset({VALIDATING, PREVIEW, APPLIED, FAILED}),
    VALIDATING: frozenset({PREVIEW, APPLIED, FAILED}),
    PREVIEW: frozenset({VALIDATING, PREVIEW, APPLIED, FAILED}),
    APPLIED: frozenset({REVERSED}),
    REVERSED: frozenset(),
    FAILED: frozenset({VALIDATING, PREVIEW, APPLIED}),
}


def transition(batch, new_status: str) -> None:
    """Move ``batch.status`` to ``new_status`` in memory, or refuse. Does not save."""
    if new_status == batch.status:
        return
    allowed = ALLOWED_TRANSITIONS.get(batch.status, frozenset())
    if new_status not in allowed:
        raise IllegalTransitionError(
            f"Batch {batch.pk} cannot move from '{batch.status}' to '{new_status}'. "
            f"Allowed from '{batch.status}': {sorted(allowed) or 'nothing — this is terminal'}."
        )
    batch.status = new_status


# ----------------------------------------------------------------- column spec


@dataclass(frozen=True)
class ImportColumn:
    """One column, in both the generated template and the parser. The one spec."""

    key: str
    header: str
    kind: str  # "text" | "date" | "time" | "decimal" | "integer" | "choice" | "boolean"
    required: bool = True
    choices: tuple[tuple[str, str], ...] | None = None
    example: object = ""
    help_text: str = ""


# ------------------------------------------------------------------- row types


@dataclass(frozen=True)
class RowIssue:
    row_number: int
    column: str
    message: str
    severity: str  # "error" | "warning"

    def as_dict(self) -> dict:
        return {
            "row": self.row_number,
            "column": self.column,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    values: dict
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchResult:
    """The generic shape every importer's result carries. A domain module adds
    its own counts by subclassing — ``below_minimum_count`` for employees,
    ``created_count``/``replaced_count`` for attendance — rather than this
    class trying to anticipate every domain's vocabulary.
    """

    row_count: int
    accepted_count: int
    rejected_count: int
    blocking_count: int
    issues: tuple[RowIssue, ...] = field(default_factory=tuple)

    @property
    def report(self) -> list[dict]:
        return [issue.as_dict() for issue in self.issues]


# --------------------------------------------------------------------- parsing


def parse_text(raw) -> str:
    return "" if raw is None else str(raw).strip()


def parse_date(raw) -> datetime.date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(raw) -> datetime.time | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime.datetime):
        return raw.time()
    if isinstance(raw, datetime.time):
        return raw
    text = str(raw).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_decimal(raw) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def parse_integer(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(Decimal(str(raw)))
    except InvalidOperation:
        return None


def parse_boolean(raw) -> bool:
    text = parse_text(raw).lower()
    return text in {"true", "yes", "1", "y", "x"}


def parse_choice(raw, column: ImportColumn) -> str | None:
    text = parse_text(raw).lower()
    if not text:
        return None
    valid = {value.lower(): value for value, _label in column.choices}
    return valid.get(text)


_PARSERS = {
    "date": parse_date,
    "time": parse_time,
    "decimal": parse_decimal,
    "integer": parse_integer,
}


def parse_cell(raw, column: ImportColumn) -> tuple[object, str | None]:
    """The typed value, and an error message if the cell cannot be used."""
    text = parse_text(raw)
    if not text and column.kind != "boolean":
        if column.required:
            return None, f"{column.header} is required."
        return ("" if column.kind == "text" else None), None

    if column.kind == "text":
        return text, None
    if column.kind == "boolean":
        return parse_boolean(raw), None
    if column.kind == "choice":
        parsed_value = parse_choice(raw, column)
        if parsed_value is None:
            allowed = ", ".join(value for value, _label in column.choices)
            return None, f"{column.header} must be one of: {allowed}. Got {text!r}."
        return parsed_value, None
    parser = _PARSERS.get(column.kind)
    if parser is None:
        raise AssertionError(f"Unknown column kind: {column.kind}")  # pragma: no cover
    parsed_value = parser(raw)
    if parsed_value is None:
        return None, f"{column.header} is not a valid {column.kind}: {text!r}."
    return parsed_value, None


def parse_workbook(
    file_like, columns: tuple[ImportColumn, ...], *, sheet_name: str
) -> list[ParsedRow]:
    """Read the data sheet against ``columns``. Matches by header text, not by
    column position, so a re-ordered (but not renamed) sheet still works.
    """
    import openpyxl

    workbook = openpyxl.load_workbook(file_like, data_only=True)
    sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.active

    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
    index_by_key: dict[str, int] = {}
    for index, header in enumerate(header_row):
        for column in columns:
            if header is not None and str(header).strip() == column.header:
                index_by_key[column.key] = index

    rows: list[ParsedRow] = []
    for row_number, raw_row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if all(cell in (None, "") for cell in raw_row):
            continue

        values: dict = {}
        errors: list[str] = []
        for column in columns:
            index = index_by_key.get(column.key)
            raw = raw_row[index] if index is not None and index < len(raw_row) else None
            value, error = parse_cell(raw, column)
            if error:
                errors.append(error)
            else:
                values[column.key] = value

        rows.append(ParsedRow(row_number=row_number, values=values, errors=tuple(errors)))

    return rows


def validation_message(error) -> str:
    """A ``django.core.exceptions.ValidationError`` as one readable string."""
    if hasattr(error, "message_dict"):
        return "; ".join(
            f"{field_name}: {' '.join(msgs)}" for field_name, msgs in error.message_dict.items()
        )
    return " ".join(error.messages)


def actor_id(actor) -> str:
    """The id of a user (or anything with a ``.pk``), stringified for a report."""
    return str(getattr(actor, "pk", actor))


# --------------------------------------------------------------- orchestration


def save_report(batch, result: BatchResult, *, extra_update_fields: tuple[str, ...] = ()) -> None:
    batch.row_count = result.row_count
    batch.accepted_count = result.accepted_count
    batch.rejected_count = result.rejected_count
    batch.validation_report = result.report
    batch.save(
        update_fields=[
            "status",
            "row_count",
            "accepted_count",
            "rejected_count",
            "validation_report",
            "updated_at",
            *extra_update_fields,
        ]
    )


def preview_batch(batch, rows: list[ParsedRow], run_rows) -> BatchResult:
    """Run every row, then roll all of it back. The batch's own report persists.
    Never raises — a blocking row is reported, not refused, until ``apply_batch``.
    """
    with transaction.atomic(), tenant_context_of(batch):
        savepoint = transaction.savepoint()
        result = run_rows(batch, rows)
        transaction.savepoint_rollback(savepoint)

        transition(batch, PREVIEW)
        save_report(batch, result)

    return result


def apply_batch(
    batch,
    rows: list[ParsedRow],
    run_rows,
    *,
    extra_refusal=None,
    on_success=None,
) -> BatchResult:
    """Run every row for real. Refuses — and writes nothing — if any row
    carries a blocking error, or if ``extra_refusal(result)`` returns a message.

    ``on_success(batch, result)``, if given, is called just before the final
    save on the accepted path, to let the caller set its own fields on
    ``batch`` (e.g. a prior-state snapshot); it returns the extra field names
    to include in the save.
    """
    with transaction.atomic(), tenant_context_of(batch):
        savepoint = transaction.savepoint()
        result = run_rows(batch, rows)

        refusal = None
        if result.blocking_count:
            refusal = (
                f"{result.blocking_count} row(s) carry a blocking error and must be "
                f"fixed before this batch can be applied. Nothing was written."
            )
        elif extra_refusal is not None:
            refusal = extra_refusal(result)

        if refusal is not None:
            transaction.savepoint_rollback(savepoint)
            transition(batch, PREVIEW)
            save_report(batch, result)
            raise ImportRefusedError(refusal)

        transaction.savepoint_commit(savepoint)
        transition(batch, APPLIED)
        batch.applied_at = timezone.now()
        extra_fields: list[str] = ["applied_at"]
        if on_success is not None:
            extra_fields.extend(on_success(batch, result) or [])
        save_report(batch, result, extra_update_fields=tuple(extra_fields))

    purge_source_file(batch)
    return result


def reverse_batch(batch, mutate) -> None:
    """Move the batch to REVERSED, run ``mutate(batch)`` to undo its writes, and
    record ``reversed_at`` — one transaction, all or nothing. ``mutate`` is
    what "undo" means for this table: delete-everything for an import that
    only ever creates, delete-and-restore for one that also replaces.
    """
    with transaction.atomic(), tenant_context_of(batch):
        transition(batch, REVERSED)
        mutate(batch)
        batch.reversed_at = timezone.now()
        batch.save(update_fields=["status", "reversed_at", "updated_at"])

    purge_source_file(batch)


def purge_source_file(batch) -> None:
    """Once a batch is terminal, the uploaded spreadsheet's content is gone
    (D-141). It held personal data in the clear, cell by cell.
    """
    if batch.source_file_id is None:
        return
    with tenant_context_of(batch):
        purge_content(batch.source_file)


# ---------------------------------------------------------------- template xlsx


def example_cell_value(column: ImportColumn):
    if column.kind == "choice" and column.example != "":
        # TextChoices values compare equal to their plain string, but openpyxl
        # writes an actual str rather than a Choices member either way.
        return str(column.example)
    if column.kind == "decimal" and isinstance(column.example, Decimal):
        return float(column.example)
    return column.example


def build_template_workbook(
    columns: tuple[ImportColumn, ...],
    *,
    data_sheet_name: str,
    instructions_sheet_name: str = "Instructions",
    validation_rows: int = 500,
):
    """The .xlsx an employer fills in, generated from ``columns`` alone.

    D-122's reason: a hand-maintained template drifts from the columns the
    importer expects, and that drift produces failures the employer cannot
    diagnose. There is exactly one column spec per importer, and both the
    template and that importer's ``parse_workbook`` call read it.
    """
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    workbook = openpyxl.Workbook()
    data_sheet = workbook.active
    data_sheet.title = data_sheet_name

    for col_index, column in enumerate(columns, start=1):
        header_cell = data_sheet.cell(row=1, column=col_index, value=column.header)
        header_cell.font = Font(bold=True)
        example_cell = data_sheet.cell(row=2, column=col_index, value=example_cell_value(column))
        if column.kind == "date":
            example_cell.number_format = "YYYY-MM-DD"
        elif column.kind == "time":
            example_cell.number_format = "HH:MM"

    for col_index, column in enumerate(columns, start=1):
        if not column.choices:
            continue
        letter = get_column_letter(col_index)
        allowed = ",".join(value for value, _label in column.choices)
        validation = DataValidation(
            type="list",
            formula1=f'"{allowed}"',
            allow_blank=not column.required,
            showErrorMessage=True,
        )
        validation.error = f"Choose one of: {allowed}"
        validation.errorTitle = "Invalid value"
        data_sheet.add_data_validation(validation)
        validation.add(f"{letter}2:{letter}{validation_rows + 1}")

    instructions_sheet = workbook.create_sheet(instructions_sheet_name)
    instructions_sheet.append(["Column", "Mandatory", "Accepts"])
    for column in columns:
        if column.choices:
            accepts = ", ".join(value for value, _label in column.choices)
        else:
            accepts = column.help_text or column.kind
        instructions_sheet.append([column.header, "Yes" if column.required else "No", accepts])
    instructions_sheet.protection.sheet = True

    return workbook
