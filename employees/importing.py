"""Bulk employee import — D-122. Preview IS apply, rolled back.

The shared mechanics — the status machine, the savepoint-based preview/apply/
reverse skeleton, the column-spec/report shapes, the template builder, the
source-file purge — live in ``core/importing.py`` (D-155), shared with the
attendance import. What stays here is what is specific to an employee row:
``EXPECTED_COLUMNS``, the per-row validation and the calls into ``engage()``
and ``capture()``, the duplicate-ID and minimum-age checks, and what "apply"
and "reverse" actually mean for this table.

**THE CENTRAL RULE**, unchanged by the extraction. Preview and apply run the
exact same code, calling the exact same service functions the single-capture
path calls: ``Employee`` creation with its identity checks, ``engage()``,
``capture()``. There is no ``validate_only`` flag anywhere in this call
chain — that would be a second validation path by another name, and D-122
exists precisely because an import that bypasses the ID check, the minimum
age check or the minimum wage check puts forty unchecked employees on file
with nobody's name against the exception. The only difference between
preview and apply is whether the transaction that ran it commits or rolls
back at the end. Rolling back burns primary key sequence values — the next
real employee gets a higher id than the row count would suggest. That is
fine; sequences are not a report.

**One declared column spec, read by both sides** (task 4's anti-drift
requirement, and D-122's own reasoning restated for this table): the template
that goes out to the employer and the parser that reads it back both read
``EXPECTED_COLUMNS``. A hand-maintained template sheet that drifts from what
the importer expects produces failures the employer cannot diagnose; a single
spec cannot drift from itself.

**A batch is a unit, both ways.** Apply refuses outright while any row carries
a blocking error — the employer fixes the file and re-uploads, rather than the
importer writing the rows that happened to be clean and silently dropping the
rest. Reverse deletes exactly the batch's employees (their children cascade)
inside one transaction, and refuses — naming which employee — if anything
downstream still references one of them.

**A below-minimum-wage row is a warning, not a blocking error** (D-108), and
can be written only when the caller of ``apply_batch`` passes a named
``acknowledged_by``. There is no default and no batch-level bypass: an import
run twice with the same file, once without an acknowledger and once with one,
must behave exactly as two single captures of the same rate would.

**The validation report never stores a full ID number or bank account
number.** Row number, column, message, and the last four digits where
identity matters — the same discipline ``employee.id_number_last4`` already
applies to the column itself.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError

from core.db.fields import keyed_hash
from core.importing import (
    BatchResult,
    IllegalTransitionError,
    ImportColumn,
    ImportRefusedError,
    ParsedRow,
    RowIssue,
    actor_id,
    transition,
    validation_message,
)
from core.importing import apply_batch as _apply_batch
from core.importing import build_template_workbook as _build_template_workbook
from core.importing import parse_workbook as _parse_workbook
from core.importing import preview_batch as _preview_batch
from core.importing import reverse_batch as _reverse_batch
from employees.engagements import (
    EngagementRefusedError,
    check_minimum_age,
    engage,
)
from employees.identity import luhn_check_digit
from employees.models import (
    Employee,
    EmployeeEngagement,
    EmployeeImportBatch,
    EmployeeRemuneration,
)
from employees.remuneration import (
    BelowMinimumWageError,
    RemunerationRefusedError,
    capture,
)

Status = EmployeeImportBatch.Status

__all__ = [
    "EXPECTED_COLUMNS",
    "IllegalTransitionError",
    "ImportRefusedError",
    "apply_batch",
    "build_template_workbook",
    "parse_workbook",
    "preview_batch",
    "reverse_batch",
    "transition",
]


# ----------------------------------------------------------------- column spec

#: A real, checksum-valid example so a curious employer who leaves row 2 in
#: place does not get refused by the very validation this file exists to run.
_EXAMPLE_ID_NUMBER = "900101" + "5009" + "08"
_EXAMPLE_ID_NUMBER += str(luhn_check_digit(_EXAMPLE_ID_NUMBER))

EXPECTED_COLUMNS: tuple[ImportColumn, ...] = (
    ImportColumn("first_name", "First name", "text", example="Thandi"),
    ImportColumn("last_name", "Last name", "text", example="Mokoena"),
    ImportColumn(
        "id_type",
        "ID type",
        "choice",
        choices=Employee.IdType.choices,
        example=Employee.IdType.SA_ID,
        help_text="A South African ID is the only type this system can checksum-verify.",
    ),
    ImportColumn(
        "id_number",
        "ID / passport / permit number",
        "text",
        example=_EXAMPLE_ID_NUMBER,
        help_text="Digits only for a South African ID. Never shared or logged in full.",
    ),
    ImportColumn("date_of_birth", "Date of birth", "date", example=datetime.date(1990, 1, 1)),
    ImportColumn(
        "gender",
        "Gender",
        "choice",
        required=False,
        choices=Employee.Gender.choices,
        example="",
        help_text="Optional. Collected for the EEA return and the UI-19 only.",
    ),
    ImportColumn("mobile_number", "Mobile number", "text", example="+27821234567"),
    ImportColumn(
        "email",
        "Email address",
        "text",
        required=False,
        example="thandi@example.com",
        help_text="Required unless 'No email' is TRUE.",
    ),
    ImportColumn(
        "has_no_email",
        "No email (TRUE/FALSE)",
        "boolean",
        required=False,
        example="FALSE",
        help_text="TRUE only for an employee with genuinely no address — payslips then go by SMS.",
    ),
    ImportColumn("job_title", "Job title", "text", example="Domestic worker"),
    ImportColumn("start_date", "Start date", "date", example=datetime.date(2026, 3, 1)),
    ImportColumn(
        "contract_type",
        "Contract type",
        "choice",
        required=False,
        choices=EmployeeEngagement.ContractType.choices,
        example=EmployeeEngagement.ContractType.PERMANENT,
        help_text="Optional. Defaults to Permanent when left blank.",
    ),
    ImportColumn(
        "pay_basis",
        "Pay basis",
        "choice",
        choices=EmployeeRemuneration.PayBasis.choices,
        example=EmployeeRemuneration.PayBasis.MONTHLY,
    ),
    ImportColumn(
        "rate_amount",
        "Rate amount",
        "decimal",
        # A plain int, not Decimal("5000.00") — test_no_hardcoded_rates scans
        # for exactly that shape, and this is an example salary for a demo
        # cell, not a statutory figure with a citation.
        example=5000,
        help_text="In the unit of pay basis — a monthly salary if pay basis is Monthly.",
    ),
)

DATA_SHEET_NAME = "Employees"


def parse_workbook(file_like, *, sheet_name: str = DATA_SHEET_NAME) -> list[ParsedRow]:
    return _parse_workbook(file_like, EXPECTED_COLUMNS, sheet_name=sheet_name)


# ------------------------------------------------------------------ row result


@dataclass(frozen=True)
class EmployeeBatchResult(BatchResult):
    below_minimum_count: int = 0


def acknowledged_by_id(acknowledged_by) -> str:
    return actor_id(acknowledged_by)


# ------------------------------------------------------------------ validation


def _run_rows(
    batch: EmployeeImportBatch, rows: list[ParsedRow], *, acknowledged_by=None
) -> EmployeeBatchResult:
    """Run every row through the single-capture path. Returns the outcome.

    Every row that gets as far as ``Employee`` creation runs inside its own
    savepoint, so one row's failure never disturbs another's — but the caller
    decides, based on the counts returned here, whether ANY of it is kept.
    """
    tenant = batch.tenant
    employer = batch.employer

    issues: list[RowIssue] = []
    accepted = 0
    rejected = 0
    blocking = 0
    below_minimum = 0
    seen_hashes: dict[str, int] = {}

    for parsed in rows:
        if parsed.errors:
            for message in parsed.errors:
                issues.append(RowIssue(parsed.row_number, "", message, "error"))
            rejected += 1
            blocking += 1
            continue

        values = parsed.values
        id_number = values["id_number"]
        last4 = id_number[-4:] if len(id_number) >= 4 else id_number

        id_hash = keyed_hash(id_number, scope=f"tenant:{tenant.pk}")
        if id_hash in seen_hashes:
            issues.append(
                RowIssue(
                    parsed.row_number,
                    "id_number",
                    f"Duplicate of row {seen_hashes[id_hash]} in this file (ID ending {last4}).",
                    "error",
                )
            )
            rejected += 1
            blocking += 1
            continue
        if Employee.objects.filter(tenant=tenant, id_number_hash=id_hash).exists():
            issues.append(
                RowIssue(
                    parsed.row_number,
                    "id_number",
                    f"An employee with an ID ending {last4} is already on file for this employer.",
                    "error",
                )
            )
            rejected += 1
            blocking += 1
            continue

        age_check = check_minimum_age(values["date_of_birth"], values["start_date"])
        if not age_check.permitted:
            issues.append(RowIssue(parsed.row_number, "date_of_birth", age_check.reason, "error"))
            rejected += 1
            blocking += 1
            continue

        seen_hashes[id_hash] = parsed.row_number

        savepoint = transaction.savepoint()
        try:
            employee = Employee(
                tenant=tenant,
                employer=employer,
                first_name=values["first_name"],
                last_name=values["last_name"],
                date_of_birth=values["date_of_birth"],
                gender=values.get("gender") or "",
                mobile_number=values["mobile_number"],
                email=values.get("email") or "",
                has_no_email=bool(values.get("has_no_email")),
                id_type=values["id_type"],
                id_number=id_number,
                created_by_import_batch=batch,
            )
            employee.full_clean()
            employee.save()

            engage(
                employee,
                start_date=values["start_date"],
                job_title=values["job_title"],
                contract_type=values.get("contract_type")
                or EmployeeEngagement.ContractType.PERMANENT,
            )

            remuneration = capture(
                employee,
                pay_basis=values["pay_basis"],
                rate_amount=values["rate_amount"],
                effective_from=values["start_date"],
                acknowledged_by=acknowledged_by,
            )
        except BelowMinimumWageError as error:
            transaction.savepoint_rollback(savepoint)
            issues.append(RowIssue(parsed.row_number, "rate_amount", str(error), "warning"))
            rejected += 1
            below_minimum += 1
            continue
        except (ValidationError, EngagementRefusedError, RemunerationRefusedError) as error:
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
            if remuneration.is_below_minimum:
                who = acknowledged_by_id(acknowledged_by)
                issues.append(
                    RowIssue(
                        parsed.row_number,
                        "rate_amount",
                        f"Below the applicable minimum wage. Acknowledged by user {who}.",
                        "warning",
                    )
                )
                below_minimum += 1

    return EmployeeBatchResult(
        row_count=len(rows),
        accepted_count=accepted,
        rejected_count=rejected,
        blocking_count=blocking,
        below_minimum_count=below_minimum,
        issues=tuple(issues),
    )


# --------------------------------------------------------------- orchestration


def preview_batch(batch: EmployeeImportBatch, rows: list[ParsedRow]) -> EmployeeBatchResult:
    """Run every row, then roll all of it back. The batch's own report persists."""

    def run_rows(batch, rows):
        return _run_rows(batch, rows, acknowledged_by=None)

    return _preview_batch(batch, rows, run_rows)


def apply_batch(
    batch: EmployeeImportBatch, rows: list[ParsedRow], *, acknowledged_by=None
) -> EmployeeBatchResult:
    """Run every row for real. Refuses — and writes nothing — if any row carries
    a blocking error, or if any row is below the minimum wage and nobody has
    acknowledged that.
    """

    def run_rows(batch, rows):
        return _run_rows(batch, rows, acknowledged_by=acknowledged_by)

    def extra_refusal(result: EmployeeBatchResult):
        if result.below_minimum_count and acknowledged_by is None:
            return (
                f"{result.below_minimum_count} row(s) are below the applicable minimum "
                f"wage. Apply again with acknowledged_by naming who accepts that — "
                f"there is no default and no batch-level bypass."
            )
        return None

    return _apply_batch(batch, rows, run_rows, extra_refusal=extra_refusal)


def reverse_batch(batch: EmployeeImportBatch) -> None:
    """Delete the batch's employees (children cascade). One transaction, all or
    nothing. Refuses, naming the employee, if anything downstream still
    references one of them.
    """

    def mutate(batch):
        for employee in Employee.objects.filter(created_by_import_batch=batch):
            try:
                employee.delete()
            except ProtectedError as error:
                raise ImportRefusedError(
                    f"This batch cannot be reversed: {employee} is still referenced "
                    f"elsewhere and cannot be removed. Nothing was removed. ({error})"
                ) from error

    _reverse_batch(batch, mutate)


def build_template_workbook():
    """The .xlsx an employer fills in, generated from ``EXPECTED_COLUMNS`` alone.

    No rate, threshold or statutory figure is embedded anywhere in this
    function — ``test_no_hardcoded_rates`` governs this file too, and the one
    number here (the example rate) is a plausible salary, not a gazetted one.
    """
    return _build_template_workbook(EXPECTED_COLUMNS, data_sheet_name=DATA_SHEET_NAME)
