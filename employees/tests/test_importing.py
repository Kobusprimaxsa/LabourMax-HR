"""Bulk employee import — D-122. Preview IS apply, rolled back.

Every test here exists because of a specific way an import goes wrong: writing
half a batch, silently skipping a below-minimum row instead of asking who
accepts it, leaking an ID number into a report, or leaving a reversed batch's
employees half-deleted. Assert on the message as well as the exception class,
every time — D-134's lesson applies here too.
"""

from __future__ import annotations

import datetime
import io
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import AppUser, FileObject, Tenant
from documents.models import Document, DocumentCategory
from employees.engagements import MINIMUM_AGE_PARAMETER
from employees.identity import luhn_check_digit
from employees.importing import (
    EXPECTED_COLUMNS,
    IllegalTransitionError,
    ImportRefusedError,
    apply_batch,
    build_template_workbook,
    parse_workbook,
    preview_batch,
    reverse_batch,
    transition,
)
from employees.models import (
    Employee,
    EmployeeEngagement,
    EmployeeImportBatch,
    EmployeePosition,
    EmployeeRemuneration,
)
from employees.remuneration import MONTHLY_FACTOR_PARAMETER
from employers.models import Employer, PayGroup
from statutory.models import MinimumWageRate, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)
NMW_HOURLY = Decimal("30.2300")

Status = EmployeeImportBatch.Status


def make_id(sequence: str) -> str:
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def parameters(db):
    for code, value, unit in (
        (MINIMUM_AGE_PARAMETER, "15.000000", StatutoryParameter.Unit.YEARS),
        (MONTHLY_FACTOR_PARAMETER, "4.333333", StatutoryParameter.Unit.RATIO),
    ):
        StatutoryParameter.objects.create(
            parameter_code=code,
            value_numeric=Decimal(value),
            unit=unit,
            effective_from=datetime.date(1997, 12, 1),
            source_reference="Basic Conditions of Employment Act 75 of 1997",
        )


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def minimum_wage(db, sector, parameters):
    return MinimumWageRate.objects.create(
        sector=None,
        hourly_rate=NMW_HOURLY,
        effective_from=START,
        source_reference="GN R.7083 in Government Gazette 54075, 3 February 2026",
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Sparkle Cleaning")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(
            tenant=tenant, trading_name="Sparkle Cleaning", sector=sector
        )


@pytest.fixture
def pay_group(db, tenant, employer):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly staff",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=START,
        )


@pytest.fixture
def acknowledger(db):
    return AppUser.objects.create_user(email="owner@example.com", password="x" * 16)


@pytest.fixture
def batch(db, tenant, employer):
    with tenant_context(tenant.pk):
        return EmployeeImportBatch.objects.create(tenant=tenant, employer=employer)


def row(sequence: str, *, first_name="Employee", rate="6000", born=BORN, **overrides) -> dict:
    values = {
        "first_name": f"{first_name}{sequence}",
        "last_name": "Test",
        "id_type": Employee.IdType.SA_ID,
        "id_number": make_id(sequence),
        "date_of_birth": born,
        "gender": "",
        "mobile_number": "+27821234567",
        "email": f"employee{sequence}@example.com",
        "has_no_email": "",
        "job_title": "Domestic worker",
        "start_date": START,
        "contract_type": EmployeeEngagement.ContractType.PERMANENT,
        "pay_basis": EmployeeRemuneration.PayBasis.MONTHLY,
        "rate_amount": Decimal(rate),
    }
    values.update(overrides)
    return values


def build_workbook(rows: list[dict]) -> io.BytesIO:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Employees"
    sheet.append([column.header for column in EXPECTED_COLUMNS])
    for values in rows:
        sheet.append([values.get(column.key, "") for column in EXPECTED_COLUMNS])

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def parsed(rows: list[dict]):
    return parse_workbook(build_workbook(rows))


def counts(tenant):
    with tenant_context(tenant.pk):
        return (
            Employee.objects.count(),
            EmployeeEngagement.objects.count(),
            EmployeePosition.objects.count(),
            EmployeeRemuneration.objects.count(),
        )


# --------------------------------------------------------------- happy path


def test_forty_rows_apply_as_a_unit(batch, tenant, pay_group, minimum_wage):
    rows = parsed([row(f"{i:04d}") for i in range(40)])

    result = apply_batch(batch, rows)

    assert result.accepted_count == 40
    assert result.rejected_count == 0
    with tenant_context(tenant.pk):
        batch.refresh_from_db()
    assert batch.status == Status.APPLIED

    with tenant_context(tenant.pk):
        employees = Employee.objects.filter(created_by_import_batch=batch)
        assert employees.count() == 40
        for employee in employees:
            assert employee.engagements.exists()
            assert employee.positions.exists()
            assert employee.remuneration.exists()


def test_the_template_columns_are_the_importers_expected_columns():
    """THE ANTI-DRIFT TEST. D-122 was argued on exactly this."""
    workbook = build_template_workbook()
    headers = [cell.value for cell in workbook["Employees"][1]]

    assert headers == [column.header for column in EXPECTED_COLUMNS]


def test_a_preview_writes_nothing(batch, tenant, pay_group, minimum_wage):
    before = counts(tenant)
    rows = parsed([row(f"{i:04d}") for i in range(5)])

    result = preview_batch(batch, rows)

    assert result.accepted_count == 5
    assert counts(tenant) == before
    with tenant_context(tenant.pk):
        batch.refresh_from_db()
    assert batch.status == Status.PREVIEW
    assert batch.accepted_count == 5


# ------------------------------------------------------------------ refusals


def test_an_underage_row_blocks_the_whole_apply(batch, tenant, pay_group, minimum_wage):
    rows = parsed(
        [
            row("0001"),
            row("0002", born=datetime.date(2015, 1, 1)),  # 11 on the start date
            row("0003"),
        ]
    )

    with pytest.raises(ImportRefusedError) as raised:
        apply_batch(batch, rows)

    assert "blocking error" in str(raised.value)
    assert counts(tenant) == (0, 0, 0, 0)


def test_a_below_minimum_row_is_a_warning_not_a_blocking_error(
    batch, tenant, pay_group, minimum_wage
):
    rows = parsed([row("0001", rate="3000")])  # hourly ~15.4, well under the NMW

    result = preview_batch(batch, rows)

    assert result.blocking_count == 0
    assert result.below_minimum_count == 1
    warnings = [i for i in result.issues if i.severity == "warning"]
    assert warnings and "below the" in warnings[0].message.lower()
    assert "minimum" in warnings[0].message.lower()


def test_applying_a_below_minimum_batch_without_acknowledgement_is_refused(
    batch, tenant, pay_group, minimum_wage
):
    rows = parsed([row("0001", rate="3000")])

    with pytest.raises(ImportRefusedError) as raised:
        apply_batch(batch, rows)

    assert "acknowledged_by" in str(raised.value)
    assert counts(tenant) == (0, 0, 0, 0)


def test_applying_with_acknowledged_by_stores_that_users_id(
    batch, tenant, pay_group, minimum_wage, acknowledger
):
    rows = parsed([row("0001", rate="3000")])

    result = apply_batch(batch, rows, acknowledged_by=acknowledger)

    assert result.accepted_count == 1
    with tenant_context(tenant.pk):
        remuneration = EmployeeRemuneration.objects.get(employee__created_by_import_batch=batch)
        assert remuneration.is_below_minimum is True
        assert remuneration.below_minimum_ack_by_user_id == acknowledger.pk


def test_an_id_already_on_file_is_a_duplicate_row_not_an_integrity_error(
    batch, tenant, pay_group, minimum_wage
):
    existing_id = make_id("9999")
    with tenant_context(tenant.pk):
        Employee.objects.create(
            tenant=tenant,
            employer=batch.employer,
            first_name="Already",
            last_name="Here",
            date_of_birth=BORN,
            mobile_number="+27821111111",
            email="already@example.com",
            id_number=existing_id,
        )

    rows = parsed([row("0001", first_name="New"), row("0002", id_number=existing_id)])

    result = preview_batch(batch, rows)

    assert result.blocking_count == 1
    duplicate_issues = [i for i in result.issues if "already on file" in i.message]
    assert len(duplicate_issues) == 1
    assert duplicate_issues[0].row_number == 3  # header + row 1 + this is the second data row

    with pytest.raises(ImportRefusedError):
        apply_batch(batch, rows)


def test_the_validation_report_contains_no_full_id_number(batch, tenant, pay_group, minimum_wage):
    existing_id = make_id("9999")
    with tenant_context(tenant.pk):
        Employee.objects.create(
            tenant=tenant,
            employer=batch.employer,
            first_name="Already",
            last_name="Here",
            date_of_birth=BORN,
            mobile_number="+27821111111",
            email="already@example.com",
            id_number=existing_id,
        )

    rows = parsed([row("0002", id_number=existing_id)])
    result = preview_batch(batch, rows)

    report_text = " ".join(issue.message for issue in result.issues)
    assert existing_id not in report_text
    assert existing_id[-4:] in report_text


# --------------------------------------------------------------------- reverse


def test_reverse_removes_exactly_the_batchs_employees(
    batch, tenant, employer, pay_group, minimum_wage
):
    rows_a = parsed([row(f"1{i:03d}") for i in range(3)])
    apply_batch(batch, rows_a)

    with tenant_context(tenant.pk):
        other_batch = EmployeeImportBatch.objects.create(tenant=tenant, employer=employer)
    rows_b = parsed([row(f"2{i:03d}") for i in range(2)])
    apply_batch(other_batch, rows_b)

    reverse_batch(batch)

    with tenant_context(tenant.pk):
        batch.refresh_from_db()
    assert batch.status == Status.REVERSED
    with tenant_context(tenant.pk):
        assert Employee.objects.filter(created_by_import_batch=batch).count() == 0
        assert Employee.objects.filter(created_by_import_batch=other_batch).count() == 2


def test_reverse_is_refused_naming_the_employee_when_referenced_downstream(
    batch, tenant, employer, pay_group, minimum_wage
):
    rows = parsed([row("0001")])
    apply_batch(batch, rows)

    with tenant_context(tenant.pk):
        employee = Employee.objects.get(created_by_import_batch=batch)
        category = DocumentCategory.objects.create(
            tenant=tenant,
            code="TEST_CATEGORY",
            name="Test",
            applies_to=DocumentCategory.AppliesTo.EMPLOYEE,
        )
        file_object = FileObject.objects.create(
            tenant=tenant,
            storage_key="documents/test-key",
            original_filename="scan.pdf",
            content_type="application/pdf",
            size_bytes=10,
            checksum_sha256="0" * 64,
            scan_status=FileObject.ScanStatus.CLEAN,
        )
        Document.objects.create(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYEE,
            employee=employee,
            document_category=category,
            file_object=file_object,
            title="ID copy",
        )

    with pytest.raises(ImportRefusedError) as raised:
        reverse_batch(batch)

    assert str(employee) in str(raised.value)
    with tenant_context(tenant.pk):
        batch.refresh_from_db()
    assert batch.status == Status.APPLIED
    with tenant_context(tenant.pk):
        assert Employee.objects.filter(pk=employee.pk).exists()


# --------------------------------------------------------------- status machine


def test_an_illegal_status_transition_is_refused():
    batch = EmployeeImportBatch(status=Status.REVERSED)

    with pytest.raises(IllegalTransitionError) as raised:
        transition(batch, Status.APPLIED)

    assert "cannot move" in str(raised.value)
    assert "reversed" in str(raised.value)
