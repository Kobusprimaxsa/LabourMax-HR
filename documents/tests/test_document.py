"""``document`` — the exclusive arc, and the rules only a related row can decide.

The arc itself is asymmetric on purpose (sheet 03): an employee- or
workplace-attached document may also name the employer, because an employee
belongs to an employer and a workplace belongs to an employer. These tests
prove each of the four arms lands correctly and that the asymmetry — employer
riding along with employee, never with tenant — is exactly what is enforced.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.managers import platform_context, tenant_context
from core.models import FileObject, Tenant
from documents.models import Document, DocumentCategory
from employees.identity import luhn_check_digit
from employees.models import Employee
from employers.models import Employer, Workplace
from statutory.models import Sector

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)


def make_id(sequence="6001"):
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def domestic(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Household")


@pytest.fixture
def employer(db, tenant, domestic):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=domestic)


@pytest.fixture
def workplace(db, tenant, employer):
    with tenant_context(tenant.pk):
        return Workplace.objects.create(tenant=tenant, employer=employer, name="The house")


@pytest.fixture
def employee(db, tenant, employer):
    with tenant_context(tenant.pk):
        return Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=BORN,
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=make_id(),
        )


def make_file(tenant):
    with tenant_context(tenant.pk):
        return FileObject.objects.create(
            tenant=tenant,
            storage_key=f"documents/{uuid.uuid4()}",
            original_filename="scan.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            checksum_sha256="0" * 64,
            scan_status=FileObject.ScanStatus.CLEAN,
        )


def make_category(**overrides):
    """A shared category for tests, stocked the way the platform stocks one."""
    values = {
        "code": f"TEST_{uuid.uuid4().hex[:8].upper()}",
        "name": "Test category",
        "applies_to": DocumentCategory.AppliesTo.ANY,
        "is_system": True,
    }
    values.update(overrides)
    with platform_context():
        return DocumentCategory.objects.create(**values)


def make_document(tenant, **fields):
    with tenant_context(tenant.pk):
        return Document.objects.create(tenant=tenant, **fields)


# ------------------------------------------------------------- the exclusive arc


def test_a_tenant_attached_document_is_valid(tenant):
    category = make_category(applies_to=DocumentCategory.AppliesTo.TENANT)
    file_object = make_file(tenant)

    document = make_document(
        tenant,
        attached_to=Document.AttachedTo.TENANT,
        document_category=category,
        file_object=file_object,
        title="Subscription agreement",
    )

    assert document.attached_to == Document.AttachedTo.TENANT
    assert document.employer_id is None
    assert document.employee_id is None
    assert document.workplace_id is None


def test_an_employer_attached_document_is_valid(tenant, employer):
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYER)
    file_object = make_file(tenant)

    document = make_document(
        tenant,
        attached_to=Document.AttachedTo.EMPLOYER,
        employer=employer,
        document_category=category,
        file_object=file_object,
        title="CIPC registration",
    )

    assert document.employer_id == employer.pk
    assert document.employee_id is None
    assert document.workplace_id is None


def test_an_employee_attached_document_is_valid(tenant, employee):
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYEE)
    file_object = make_file(tenant)

    document = make_document(
        tenant,
        attached_to=Document.AttachedTo.EMPLOYEE,
        employee=employee,
        document_category=category,
        file_object=file_object,
        title="ID copy",
    )

    assert document.employee_id == employee.pk
    assert document.workplace_id is None


def test_a_workplace_attached_document_is_valid(tenant, workplace):
    category = make_category(applies_to=DocumentCategory.AppliesTo.WORKPLACE)
    file_object = make_file(tenant)

    document = make_document(
        tenant,
        attached_to=Document.AttachedTo.WORKPLACE,
        workplace=workplace,
        document_category=category,
        file_object=file_object,
        title="Client contract",
    )

    assert document.workplace_id == workplace.pk
    assert document.employee_id is None


def test_an_employee_document_may_also_carry_the_employer(tenant, employer, employee):
    """The arc is asymmetric on purpose: an employee belongs to an employer, so
    the document may name both.
    """
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYEE)
    file_object = make_file(tenant)

    document = make_document(
        tenant,
        attached_to=Document.AttachedTo.EMPLOYEE,
        employee=employee,
        employer=employer,
        document_category=category,
        file_object=file_object,
        title="Police clearance",
    )

    assert document.employee_id == employee.pk
    assert document.employer_id == employer.pk


def test_employee_attached_with_no_employee_id_is_refused(tenant):
    category = make_category(applies_to=DocumentCategory.AppliesTo.ANY)
    file_object = make_file(tenant)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        make_document(
            tenant,
            attached_to=Document.AttachedTo.EMPLOYEE,
            document_category=category,
            file_object=file_object,
            title="Orphaned",
        )

    assert "document_exclusive_arc" in str(raised.value)


def test_workplace_attached_with_an_employee_id_is_refused(tenant, workplace, employee):
    """The last two arms are asymmetric only one way: workplace may not also
    carry employee_id, though employee may carry employer_id.
    """
    category = make_category(applies_to=DocumentCategory.AppliesTo.ANY)
    file_object = make_file(tenant)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        make_document(
            tenant,
            attached_to=Document.AttachedTo.WORKPLACE,
            workplace=workplace,
            employee=employee,
            document_category=category,
            file_object=file_object,
            title="Mismatched",
        )

    assert "document_exclusive_arc" in str(raised.value)


# ----------------------------------------------------------------- clean() rules


def test_a_category_requiring_expiry_refuses_a_document_without_one(tenant, employee):
    category = make_category(
        applies_to=DocumentCategory.AppliesTo.EMPLOYEE,
        requires_expiry_date=True,
        code="TEST_WORK_PERMIT",
    )
    file_object = make_file(tenant)

    with tenant_context(tenant.pk):
        document = Document(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYEE,
            employee=employee,
            document_category=category,
            file_object=file_object,
            title="Work permit",
        )
        with pytest.raises(ValidationError) as raised:
            document.full_clean()

    assert "requires an expiry date" in str(raised.value)


def test_a_category_mismatched_with_attached_to_is_refused(tenant, workplace):
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYEE, code="TEST_EMP_ONLY")
    file_object = make_file(tenant)

    with tenant_context(tenant.pk):
        document = Document(
            tenant=tenant,
            attached_to=Document.AttachedTo.WORKPLACE,
            workplace=workplace,
            document_category=category,
            file_object=file_object,
            title="Safety file",
        )
        with pytest.raises(ValidationError) as raised:
            document.full_clean()

    assert "applies to" in str(raised.value)


def test_a_contract_cleaning_only_category_is_refused_for_a_household_employer(
    tenant, employer, cleaning
):
    """CIPC and BEE are not household documents — sheet 03's own example."""
    category = make_category(
        applies_to=DocumentCategory.AppliesTo.EMPLOYER, sector=cleaning, code="TEST_CIPC"
    )
    file_object = make_file(tenant)

    with tenant_context(tenant.pk):
        document = Document(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYER,
            employer=employer,
            document_category=category,
            file_object=file_object,
            title="CIPC registration",
        )
        with pytest.raises(ValidationError) as raised:
            document.full_clean()

    assert "not in it" in str(raised.value)


def test_a_document_cannot_supersede_itself(tenant, employer):
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYER)
    file_object = make_file(tenant)

    with tenant_context(tenant.pk):
        document = Document.objects.create(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYER,
            employer=employer,
            document_category=category,
            file_object=file_object,
            title="Original",
        )
        document.supersedes_document = document
        with pytest.raises(ValidationError) as raised:
            document.full_clean()

    assert "cannot supersede itself" in str(raised.value)


def test_a_renewal_chain_is_legitimate_but_a_cycle_is_refused(tenant, employer):
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYER)

    with tenant_context(tenant.pk):
        permit_2024 = Document.objects.create(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYER,
            employer=employer,
            document_category=category,
            file_object=make_file(tenant),
            title="2024 permit",
        )
        permit_2026 = Document.objects.create(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYER,
            employer=employer,
            document_category=category,
            file_object=make_file(tenant),
            title="2026 permit",
            supersedes_document=permit_2024,
        )
        permit_2028 = Document(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYER,
            employer=employer,
            document_category=category,
            file_object=make_file(tenant),
            title="2028 permit",
            supersedes_document=permit_2026,
        )
        permit_2028.full_clean()  # a chain is legitimate and must not raise
        permit_2028.save()

        # Now close the loop: make the 2024 permit claim to supersede the 2028
        # one, so walking from 2028 eventually comes back to 2028.
        permit_2024.supersedes_document = permit_2028
        with pytest.raises(ValidationError) as raised:
            permit_2024.full_clean()

    assert "cycle" in str(raised.value)


def test_verified_by_and_at_must_be_set_together(tenant, employer):
    category = make_category(applies_to=DocumentCategory.AppliesTo.EMPLOYER)
    file_object = make_file(tenant)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        make_document(
            tenant,
            attached_to=Document.AttachedTo.EMPLOYER,
            employer=employer,
            document_category=category,
            file_object=file_object,
            title="Half verified",
            verified_at=datetime.datetime.now(datetime.UTC),
        )

    assert "document_verified_by_and_at_together" in str(raised.value)
