"""Bulk attendance import — sheet 02/03, P5 chunk 3 (D-155, D-156).

The shared mechanics — status machine, savepoint-based preview/apply/reverse
skeleton, column-spec/report shapes, template builder, source-file purge —
live in ``core/importing.py``, shared with the employee import (D-155). What
is specific to this table is what a row means and, unlike the employee
import, what "apply" and "reverse" actually do — because this importer
routinely REPLACES a day that already exists, and the employee import never
does.

**The importer supplies RAW CAPTURE ONLY.** Every row is written through
``attendance/capture.py::capture()`` — the exact function manual entry
calls — so hour bucketing, caps and exceptions are computed by
``calculators/attendance.py`` from the rule set in force on the work date,
identically to manual capture. The template therefore carries none of
``ordinary_hours``, ``overtime_hours``, ``sunday_hours``,
``public_holiday_hours`` or ``night_hours`` — a spreadsheet column writing a
bucket directly would be a route around the bucketing, the caps and the
exceptions, and ``test_the_template_carries_no_bucket_column`` asserts on
``EXPECTED_COLUMNS`` itself so this stays true if a column is ever added.

``standby_hours_worked`` is deliberately NOT a template column either, even
though the task that specified this importer named it alongside
``is_standby``. It is not a raw capture value: ``calculators/attendance.py``
computes it from the same worked-hours span every other bucket comes from
(``bucket_day``: ``standby_worked = hours_worked`` when ``is_standby`` is
set) — there is no slot in ``AttendanceDayInput`` for a caller to hand it in
directly, and adding one only to have the importer write straight into a
bucket the calculator already derives would be exactly the drift this
module's own docstring warns against. ``is_standby`` is the raw capture; the
hours are read off the same time span (or ``hours_worked`` figure) as any
other day.

**Locked and approved days are guarded before ``capture()`` is ever called**,
not left to its own ``LockedDayError`` or to the ``no_update_when_locked``
trigger. A locked day is refused by name in the row loop, so preview reports
it — it must never merely die on a trigger at apply time. An approved day is
refused the same way unless the caller passes
``allow_replacing_approved=True``: approval is a human judgement call, and an
import silently overwriting it is the same class of wrong as editing a
finalised payslip.

**Reverse restores, it does not only delete** (D-156). The employee import
only ever creates, so its reverse deletes everything it made. This importer
replaces days that already held the employer's own earlier, legitimate
capture — deleting on reverse would throw that away, which the batch has no
right to do. Every REPLACE therefore snapshots the day's pre-replace values
into ``batch.prior_state`` before ``capture()`` overwrites them; reverse
restores each one exactly and deletes only the days the batch created
outright.

**A bare ``hours_worked`` figure cannot carry night pay, and this importer
says so rather than staying silent** (D-157). ``capture()`` surfaces the
calculator's own warnings on the row it just wrote; every one is added to
this batch's report as a WARNING issue, never blocking — the day itself is
still accepted, exactly as the grid's single-cell capture would accept it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from attendance.capture import AttendanceCaptureRefusedError, capture
from attendance.models import AttendanceDay, AttendanceImportBatch
from core.importing import (
    BatchResult,
    ImportColumn,
    ImportRefusedError,
    ParsedRow,
    RowIssue,
    validation_message,
)
from core.importing import apply_batch as _apply_batch
from core.importing import build_template_workbook as _build_template_workbook
from core.importing import parse_workbook as _parse_workbook
from core.importing import preview_batch as _preview_batch
from core.importing import reverse_batch as _reverse_batch
from employees.models import Employee
from employers.models import Workplace

Status = AttendanceImportBatch.Status

__all__ = [
    "EXPECTED_COLUMNS",
    "ImportRefusedError",
    "apply_batch",
    "build_template_workbook",
    "parse_workbook",
    "preview_batch",
    "reverse_batch",
]


# ----------------------------------------------------------------- column spec

#: Every day type except LEAVE. A leave day needs a leave_application — set
#: only by leave/authorisation.py approving one (P6 chunk 2) — so it is not
#: an importable form of raw capture. The bulk importer captures WORK, not
#: leave; a day off is recorded by applying for it, not by uploading a row.
_IMPORTABLE_DAY_TYPES = tuple(
    (value, label)
    for value, label in AttendanceDay.DayType.choices
    if value != AttendanceDay.DayType.LEAVE
)

EXPECTED_COLUMNS: tuple[ImportColumn, ...] = (
    ImportColumn(
        "employee_number",
        "Employee number",
        "text",
        help_text="The employer's own key. Never the ID number — this file gets emailed.",
    ),
    ImportColumn(
        "first_name",
        "First name",
        "text",
        help_text="For readability, cross-checked against the employee number — not used as a key.",
    ),
    ImportColumn(
        "last_name",
        "Last name",
        "text",
        help_text="For readability, cross-checked against the employee number — not used as a key.",
    ),
    ImportColumn("work_date", "Work date", "date", example=datetime.date(2026, 3, 2)),
    ImportColumn(
        "day_type",
        "Day type",
        "choice",
        choices=_IMPORTABLE_DAY_TYPES,
        example=AttendanceDay.DayType.ORDINARY,
    ),
    ImportColumn(
        "time_in",
        "Time in",
        "time",
        required=False,
        example=datetime.time(8, 0),
        help_text="With time out and unpaid break. Or leave all three blank and use hours worked.",
    ),
    ImportColumn(
        "time_out",
        "Time out",
        "time",
        required=False,
        example=datetime.time(17, 0),
        help_text="With time in and unpaid break. Or leave all three blank and use hours worked.",
    ),
    ImportColumn(
        "unpaid_break_minutes",
        "Unpaid break (minutes)",
        "integer",
        required=False,
        example=60,
        help_text="Only with time in/time out. Leave blank when using hours worked.",
    ),
    ImportColumn(
        "hours_worked",
        "Hours worked",
        "decimal",
        required=False,
        help_text="Instead of time in/time out — provide exactly one of the two forms.",
    ),
    ImportColumn(
        "workplace",
        "Workplace",
        "text",
        required=False,
        help_text="The site's name on file. Leave blank if this employer has none recorded.",
    ),
    ImportColumn(
        "is_standby",
        "Standby (TRUE/FALSE)",
        "boolean",
        required=False,
        example="FALSE",
    ),
    ImportColumn("comment", "Comment", "text", required=False),
)

DATA_SHEET_NAME = "Attendance"


def parse_workbook(file_like, *, sheet_name: str = DATA_SHEET_NAME) -> list[ParsedRow]:
    return _parse_workbook(file_like, EXPECTED_COLUMNS, sheet_name=sheet_name)


# ------------------------------------------------------------------ row result


@dataclass(frozen=True)
class AttendanceBatchResult(BatchResult):
    created_count: int = 0
    replaced_count: int = 0
    #: Not part of `.report` — the pre-replace snapshot for each REPLACED day,
    #: persisted onto the batch only when apply_batch actually commits.
    prior_state: tuple[dict, ...] = field(default_factory=tuple)


#: Every AttendanceDay column capture() can touch, snapshotted before a
#: REPLACE so reverse can restore it exactly (D-156). tenant/employee/work_date
#: are the row's identity and never change; everything else is fair game.
_SNAPSHOT_FIELDS = (
    "workplace_id",
    "day_type",
    "leave_application_id",
    "time_in",
    "time_out",
    "unpaid_break_minutes",
    "ordinary_hours",
    "overtime_hours",
    "sunday_hours",
    "public_holiday_hours",
    "night_hours",
    "paid_hours_guaranteed",
    "days_worked_equivalent",
    "is_standby",
    "standby_hours_worked",
    "source",
    "status",
    "comment",
    "import_batch_id",
)
_DECIMAL_SNAPSHOT_FIELDS = frozenset(
    {
        "ordinary_hours",
        "overtime_hours",
        "sunday_hours",
        "public_holiday_hours",
        "night_hours",
        "paid_hours_guaranteed",
        "days_worked_equivalent",
        "standby_hours_worked",
    }
)
_TIME_SNAPSHOT_FIELDS = frozenset({"time_in", "time_out"})


def _serialize_snapshot_value(value):
    if isinstance(value, (Decimal, datetime.time)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    return value


def _snapshot_day(day: AttendanceDay) -> dict:
    return {name: _serialize_snapshot_value(getattr(day, name)) for name in _SNAPSHOT_FIELDS}


def _restore_day(day: AttendanceDay, fields: dict) -> None:
    for name, raw in fields.items():
        if raw is not None and name in _DECIMAL_SNAPSHOT_FIELDS:
            value = Decimal(raw)
        elif raw is not None and name in _TIME_SNAPSHOT_FIELDS:
            value = datetime.time.fromisoformat(raw)
        else:
            value = raw
        setattr(day, name, value)


# ------------------------------------------------------------------ validation


def _exactly_one_capture_form(values: dict) -> str | None:
    """None if the row gives exactly one of (time span) / (hours worked);
    otherwise the error message.
    """
    has_time_in = values.get("time_in") is not None
    has_time_out = values.get("time_out") is not None
    has_hours = values.get("hours_worked") is not None

    if has_hours and (has_time_in or has_time_out):
        return "Provide either time in/time out or hours worked, not both."
    if not has_hours and not (has_time_in and has_time_out):
        if has_time_in or has_time_out:
            return "Time in and time out are both required when using a clock in/out."
        return "Provide either time in/time out or hours worked."
    return None


def _run_rows(
    batch: AttendanceImportBatch, rows: list[ParsedRow], *, allow_replacing_approved: bool
) -> AttendanceBatchResult:
    """Run every row through the single-day capture path. Returns the outcome.

    Every row that gets as far as ``capture()`` runs inside its own savepoint,
    exactly as the employee import's rows do — one row's failure never
    disturbs another's, and the caller decides whether ANY of it is kept.
    """
    tenant = batch.tenant
    employer = batch.employer

    issues: list[RowIssue] = []
    accepted = 0
    rejected = 0
    blocking = 0
    created = 0
    replaced = 0
    snapshot_entries: list[dict] = []

    for parsed in rows:
        if parsed.errors:
            for message in parsed.errors:
                issues.append(RowIssue(parsed.row_number, "", message, "error"))
            rejected += 1
            blocking += 1
            continue

        values = parsed.values

        form_error = _exactly_one_capture_form(values)
        if form_error:
            issues.append(RowIssue(parsed.row_number, "hours_worked", form_error, "error"))
            rejected += 1
            blocking += 1
            continue

        employee = Employee.objects.filter(
            tenant=tenant, employer=employer, employee_number=values["employee_number"]
        ).first()
        if employee is None:
            issues.append(
                RowIssue(
                    parsed.row_number,
                    "employee_number",
                    f"No employee numbered {values['employee_number']!r} on file for this "
                    f"employer.",
                    "error",
                )
            )
            rejected += 1
            blocking += 1
            continue

        given_first = values["first_name"].strip().lower()
        given_last = values["last_name"].strip().lower()
        if given_first != employee.first_name.strip().lower() or (
            given_last != employee.last_name.strip().lower()
        ):
            issues.append(
                RowIssue(
                    parsed.row_number,
                    "first_name",
                    f"{values['first_name']} {values['last_name']} does not match employee "
                    f"{values['employee_number']} on file ({employee.first_name} "
                    f"{employee.last_name}).",
                    "error",
                )
            )
            rejected += 1
            blocking += 1
            continue

        workplace = None
        workplace_name = values.get("workplace") or ""
        if workplace_name:
            workplace = Workplace.objects.filter(
                tenant=tenant, employer=employer, name__iexact=workplace_name
            ).first()
            if workplace is None:
                issues.append(
                    RowIssue(
                        parsed.row_number,
                        "workplace",
                        f"No workplace named {workplace_name!r} on file for this employer.",
                        "error",
                    )
                )
                rejected += 1
                blocking += 1
                continue

        work_date = values["work_date"]
        existing = AttendanceDay.objects.filter(employee=employee, work_date=work_date).first()
        if existing is not None and existing.status == AttendanceDay.Status.LOCKED:
            issues.append(
                RowIssue(
                    parsed.row_number,
                    "work_date",
                    f"{work_date:%d %B %Y} for {employee} is locked by payroll run "
                    f"{existing.locked_by_payroll_run_id_ref} and will not be replaced.",
                    "error",
                )
            )
            rejected += 1
            blocking += 1
            continue
        if (
            existing is not None
            and existing.status == AttendanceDay.Status.APPROVED
            and not allow_replacing_approved
        ):
            issues.append(
                RowIssue(
                    parsed.row_number,
                    "work_date",
                    f"{work_date:%d %B %Y} for {employee} is already approved and will not be "
                    f"replaced. Apply again with allow_replacing_approved=True to override.",
                    "error",
                )
            )
            rejected += 1
            blocking += 1
            continue

        is_replace = existing is not None
        snapshot = _snapshot_day(existing) if is_replace else None

        savepoint = transaction.savepoint()
        try:
            captured_day = capture(
                employee,
                work_date=work_date,
                day_type=values["day_type"],
                workplace=workplace,
                time_in=values.get("time_in"),
                time_out=values.get("time_out"),
                unpaid_break_minutes=values.get("unpaid_break_minutes") or 0,
                hours_worked=values.get("hours_worked"),
                is_standby=bool(values.get("is_standby")),
                source=AttendanceDay.Source.IMPORT,
                comment=values.get("comment") or "",
                import_batch=batch,
                # Checked above, by name, before this call: passing it through is
                # the importer's decision reaching the one write path (D-297).
                allow_replacing_approved=allow_replacing_approved,
            )
        except (ValidationError, AttendanceCaptureRefusedError) as error:
            transaction.savepoint_rollback(savepoint)
            message = (
                validation_message(error) if isinstance(error, ValidationError) else str(error)
            )
            issues.append(RowIssue(parsed.row_number, "", message, "error"))
            rejected += 1
            blocking += 1
            continue
        else:
            transaction.savepoint_commit(savepoint)
            accepted += 1
            for warning in captured_day.capture_warnings:
                issues.append(RowIssue(parsed.row_number, "hours_worked", warning, "warning"))
            if is_replace:
                replaced += 1
                snapshot_entries.append(
                    {
                        "employee_id": employee.pk,
                        "work_date": work_date.isoformat(),
                        "fields": snapshot,
                    }
                )
            else:
                created += 1

    return AttendanceBatchResult(
        row_count=len(rows),
        accepted_count=accepted,
        rejected_count=rejected,
        blocking_count=blocking,
        created_count=created,
        replaced_count=replaced,
        issues=tuple(issues),
        prior_state=tuple(snapshot_entries),
    )


# --------------------------------------------------------------- orchestration


def preview_batch(
    batch: AttendanceImportBatch, rows: list[ParsedRow], *, allow_replacing_approved: bool = False
) -> AttendanceBatchResult:
    """Run every row, then roll all of it back. The batch's own report persists.

    Runs with the same ``allow_replacing_approved`` the caller intends to
    apply with, so a locked or (unflagged) approved day is refused by name
    here rather than surfacing for the first time at apply.
    """

    def run_rows(batch, rows):
        return _run_rows(batch, rows, allow_replacing_approved=allow_replacing_approved)

    return _preview_batch(batch, rows, run_rows)


def apply_batch(
    batch: AttendanceImportBatch, rows: list[ParsedRow], *, allow_replacing_approved: bool = False
) -> AttendanceBatchResult:
    """Run every row for real. Refuses — and writes nothing — if any row
    carries a blocking error: a parse failure, an unknown employee or
    workplace, a name that does not match the employee number, a locked day,
    or an approved day with ``allow_replacing_approved`` not set.
    """

    def run_rows(batch, rows):
        return _run_rows(batch, rows, allow_replacing_approved=allow_replacing_approved)

    def on_success(batch, result: AttendanceBatchResult):
        batch.prior_state = list(result.prior_state)
        return ["prior_state"]

    return _apply_batch(batch, rows, run_rows, on_success=on_success)


def reverse_batch(batch: AttendanceImportBatch) -> None:
    """Restore every day this batch replaced to exactly what it held before,
    and delete every day it created outright. One transaction, all or
    nothing.
    """

    def mutate(batch):
        prior_by_key = {
            (entry["employee_id"], entry["work_date"]): entry["fields"]
            for entry in batch.prior_state
        }
        for day in AttendanceDay.objects.filter(import_batch=batch):
            key = (day.employee_id, day.work_date.isoformat())
            fields = prior_by_key.get(key)
            if fields is None:
                day.delete()
            else:
                _restore_day(day, fields)
                day.full_clean()
                day.save()

    _reverse_batch(batch, mutate)


def build_template_workbook():
    """The .xlsx an employer fills in, generated from ``EXPECTED_COLUMNS``
    alone — the same spec ``parse_workbook`` reads (D-122's reasoning,
    restated for this table).
    """
    return _build_template_workbook(EXPECTED_COLUMNS, data_sheet_name=DATA_SHEET_NAME)
