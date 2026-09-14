"""The employee self-service whitelist — the one place either audience's filter
lives (task 3 / sheet 03).
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from core.managers import platform_context, tenant_context
from core.models import FileObject, Tenant
from documents.categories import seed_system_categories
from documents.models import Document, DocumentCategory
from documents.visibility import visible_to_employee
from employees.identity import luhn_check_digit
from employees.models import Employee
from employers.models import Employer
from statutory.models import Sector

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)


def make_id(sequence="7001"):
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


def file_for(tenant, employee, category, title):
    with tenant_context(tenant.pk):
        return Document.objects.create(
            tenant=tenant,
            attached_to=Document.AttachedTo.EMPLOYEE,
            employee=employee,
            document_category=category,
            file_object=make_file(tenant),
            title=title,
        )


def test_the_whitelist_returns_the_employment_contract_and_nothing_else(tenant, employee, cleaning):
    seed_system_categories()
    contract = DocumentCategory.objects.get(code="EMPLOYMENT_CONTRACT")
    id_copy = DocumentCategory.objects.get(code="ID_COPY")

    file_for(tenant, employee, contract, "Signed contract")
    file_for(tenant, employee, id_copy, "ID copy")  # visible_to_employee is False

    visible = visible_to_employee(employee)

    assert [d.title for d in visible] == ["Signed contract"]


def test_a_category_added_later_with_the_flag_unset_stays_hidden(tenant, employee, cleaning):
    """A whitelist, default deny: a category nobody has decided on yet is not
    shown just because it exists.
    """
    seed_system_categories()
    contract = DocumentCategory.objects.get(code="EMPLOYMENT_CONTRACT")
    file_for(tenant, employee, contract, "Signed contract")

    with platform_context():
        new_category = DocumentCategory.objects.create(
            code="BRAND_NEW",
            name="Added after the fact",
            applies_to=DocumentCategory.AppliesTo.EMPLOYEE,
            is_system=True,
        )
    file_for(tenant, employee, new_category, "Should stay hidden")

    visible = visible_to_employee(employee)

    assert [d.title for d in visible] == ["Signed contract"]
