"""File storage — the scan gate, the audit trail, and what is refused.

Every test here exists because of a specific way file handling leaks data:
serving an unscanned upload, building a path from an attacker-controlled
filename, trusting the client's declared content type, or letting a user destroy
a record the employer is legally required to keep.
"""

from __future__ import annotations

import io

import pytest

from core.files import (
    ALLOWED_CONTENT_TYPES,
    DownloadRefusedError,
    FileRejectedError,
    assert_downloadable,
    open_for_download,
    record_scan_result,
    sniff_content_type,
    soft_delete,
    storage_key_for,
    store,
)
from core.managers import platform_context, tenant_context
from core.models import AuditLog, FileObject, Tenant

PDF = b"%PDF-1.7\n%\xc7\xec\x8f\xa2\n1 0 obj\n<<>>\nendobj\ntrailer\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
DOCX = b"PK\x03\x04" + b"\x00" * 64


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Files Co")


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def upload(data: bytes, name: str):
    handle = io.BytesIO(data)
    handle.name = name
    return handle


def stored(tenant, data=PDF, name="id-document.pdf"):
    with tenant_context(tenant.pk):
        return store(tenant_id=tenant.pk, upload=upload(data, name))


# ------------------------------------------------------------------ what is refused


@pytest.mark.files
def test_an_empty_file_is_refused(db, tenant, media):
    with tenant_context(tenant.pk), pytest.raises(FileRejectedError, match="empty"):
        store(tenant_id=tenant.pk, upload=upload(b"", "empty.pdf"))


@pytest.mark.files
def test_an_oversized_file_is_refused_before_anything_is_written(db, tenant, media, settings):
    settings.FILE_UPLOAD_MAX_BYTES = 1024
    with tenant_context(tenant.pk), pytest.raises(FileRejectedError, match="maximum"):
        store(tenant_id=tenant.pk, upload=upload(PDF + b"\x00" * 4096, "big.pdf"))

    assert list(media.rglob("*")) == [], "Bytes were written despite the refusal."
    with platform_context():
        assert FileObject.all_tenants.count() == 0


@pytest.mark.files
def test_the_declared_extension_cannot_launder_the_real_type(db, tenant, media):
    """A Windows executable renamed to .pdf is still a Windows executable."""
    with tenant_context(tenant.pk), pytest.raises(FileRejectedError, match="not accepted"):
        store(tenant_id=tenant.pk, upload=upload(b"MZ\x90\x00" + b"\x00" * 64, "invoice.pdf"))


@pytest.mark.files
def test_a_zip_of_anything_is_not_accepted_as_an_office_document(db, tenant, media):
    with tenant_context(tenant.pk), pytest.raises(FileRejectedError):
        store(tenant_id=tenant.pk, upload=upload(DOCX, "payload.zip"))


@pytest.mark.files
def test_office_formats_are_told_apart_by_extension_only(db, tenant, media):
    """They share one signature, so the extension is the only signal available."""
    assert sniff_content_type(DOCX, "contract.docx").endswith("wordprocessingml.document")
    assert sniff_content_type(DOCX, "hours.xlsx").endswith("spreadsheetml.sheet")
    assert sniff_content_type(DOCX, "anything.zip") is None


@pytest.mark.files
def test_binary_masquerading_as_text_is_refused():
    assert sniff_content_type(b"\xff\xfe\x00\x01binary", "notes.txt") is None
    assert sniff_content_type(b"Employee,Hours\n", "hours.csv") == "text/csv"


@pytest.mark.files
def test_every_sniffed_type_is_on_the_allowlist():
    """Guards against a signature being added without the allowlist following."""
    for data, name in [(PDF, "a.pdf"), (PNG, "a.png"), (DOCX, "a.docx"), (b"text", "a.txt")]:
        assert sniff_content_type(data, name) in ALLOWED_CONTENT_TYPES


# ------------------------------------------------------------------ the storage key


@pytest.mark.files
def test_the_storage_key_carries_nothing_about_the_file(db, tenant, media):
    """A filename is personal information and attacker-controlled. Both matter."""
    file_object = stored(tenant, PDF, "Thabo Mokoena ID copy.pdf")

    assert "Thabo" not in file_object.storage_key
    assert "Mokoena" not in file_object.storage_key
    assert ".pdf" not in file_object.storage_key
    assert file_object.original_filename == "Thabo Mokoena ID copy.pdf"


@pytest.mark.files
def test_a_traversing_filename_cannot_escape_the_tenant_prefix(db, tenant, media):
    file_object = stored(tenant, PDF, "../../../../etc/passwd.pdf")
    assert file_object.storage_key.startswith(f"tenant/{tenant.pk}/")
    assert ".." not in file_object.storage_key


@pytest.mark.files
def test_keys_are_unguessable_and_unique(db, tenant):
    keys = {storage_key_for(tenant.pk) for _ in range(50)}
    assert len(keys) == 50


# ------------------------------------------------------------------ the scan gate


@pytest.mark.files
def test_a_new_upload_is_pending_and_not_downloadable(db, tenant, media):
    file_object = stored(tenant)
    assert file_object.scan_status == FileObject.ScanStatus.PENDING

    with tenant_context(tenant.pk), pytest.raises(DownloadRefusedError, match="being checked"):
        assert_downloadable(file_object)


@pytest.mark.files
def test_a_clean_file_is_downloadable_and_the_bytes_come_back(db, tenant, media):
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.CLEAN)

    with tenant_context(tenant.pk):
        with open_for_download(file_object) as handle:
            assert handle.read() == PDF


@pytest.mark.files
def test_an_infected_file_is_never_served(db, tenant, media):
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.INFECTED)

    with tenant_context(tenant.pk), pytest.raises(DownloadRefusedError, match="virus scan"):
        open_for_download(file_object)


@pytest.mark.files
def test_an_infected_verdict_is_terminal(db, tenant, media):
    """A re-scan against broken definitions must not turn malware clean.

    The costs are not symmetric: asking an employer to upload again is an
    inconvenience, serving them malware is not.
    """
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.INFECTED)

    with pytest.raises(FileRejectedError, match="cannot be re-scanned"):
        record_scan_result(file_object, FileObject.ScanStatus.CLEAN)


@pytest.mark.files
def test_a_failed_scan_is_not_treated_as_clean(db, tenant, media):
    """The scanner erroring is not the same as the scanner approving."""
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.FAILED)

    with tenant_context(tenant.pk), pytest.raises(DownloadRefusedError):
        assert_downloadable(file_object)


# ------------------------------------------------------------------ the audit trail


@pytest.mark.files
def test_every_download_writes_an_audit_row(db, tenant, media):
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.CLEAN)

    with tenant_context(tenant.pk):
        open_for_download(file_object).close()
        open_for_download(file_object, business_event="payslip_downloaded").close()

        reads = AuditLog.objects.filter(
            table_name="file_object",
            record_pk=file_object.pk,
            operation=AuditLog.Operation.READ,
        )
        assert reads.count() == 2
        assert {r.business_event for r in reads} == {"file_downloaded", "payslip_downloaded"}


@pytest.mark.files
def test_a_refused_download_writes_no_read_row(db, tenant, media):
    """The row means "this was disclosed". A refusal disclosed nothing."""
    file_object = stored(tenant)

    with tenant_context(tenant.pk):
        with pytest.raises(DownloadRefusedError):
            open_for_download(file_object)

        assert not AuditLog.objects.filter(
            record_pk=file_object.pk, operation=AuditLog.Operation.READ
        ).exists()


@pytest.mark.files
def test_the_read_row_belongs_to_the_files_tenant(db, tenant, media):
    other = Tenant.objects.create(trading_name="Other Files Co")
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.CLEAN)

    with tenant_context(tenant.pk):
        open_for_download(file_object).close()

    with tenant_context(other.pk):
        assert not AuditLog.objects.filter(
            record_pk=file_object.pk, operation=AuditLog.Operation.READ
        ).exists()


# ------------------------------------------------------------------ deletion


@pytest.mark.files
def test_delete_is_soft_and_the_bytes_survive(db, tenant, media):
    """Statutory retention outlasts the customer relationship.

    A user pressing delete must not be able to destroy a record the employer is
    legally required to hold for five years.
    """
    file_object = stored(tenant)
    record_scan_result(file_object, FileObject.ScanStatus.CLEAN)
    key = file_object.storage_key

    with tenant_context(tenant.pk):
        soft_delete(file_object)

    from django.core.files.storage import default_storage

    assert default_storage.exists(key), "The bytes were destroyed by a soft delete."

    with tenant_context(tenant.pk), pytest.raises(DownloadRefusedError, match="no longer"):
        open_for_download(file_object)


@pytest.mark.files
def test_a_file_belongs_to_exactly_one_tenant(db, tenant, media):
    """Decision D-52, at the level the storage layer sees it."""
    other = Tenant.objects.create(trading_name="Nosy Co")
    file_object = stored(tenant)

    with tenant_context(other.pk):
        assert not FileObject.objects.filter(pk=file_object.pk).exists()

    with tenant_context(tenant.pk):
        assert FileObject.objects.filter(pk=file_object.pk).exists()


@pytest.mark.files
def test_the_file_service_works_with_no_tenant_pinned(db, tenant, media):
    """Regression: every one of these wrote through a FORCED RLS policy.

    ``record_scan_result`` and ``soft_delete`` used ``save(update_fields=...)``
    with no tenant pinned, so the UPDATE matched nothing. Django checks the
    affected row count when ``update_fields`` is given, which is the only reason
    it failed loudly rather than silently doing nothing — a plain ``save()``
    would have reported success and changed no row.

    A scanner callback, a Celery task and a retention job all arrive with no
    request and therefore no tenant context, so this is the ordinary path.

    Note where the contexts sit. The **service calls** are deliberately outside
    any context, because pinning is their job now. The **plain ORM reads** are
    inside one, because that is not a bug to be fixed: a tenant-scoped row read
    from nowhere must return nothing. Writing this test without that distinction
    is what produced a DoesNotExist on the first run.
    """
    file_object = store(tenant_id=tenant.pk, upload=upload(PDF, "no-context.pdf"))
    record_scan_result(file_object, FileObject.ScanStatus.CLEAN)

    with tenant_context(tenant.pk):
        file_object.refresh_from_db()
        assert file_object.scan_status == FileObject.ScanStatus.CLEAN

    with open_for_download(file_object) as handle:
        assert handle.read() == PDF

    soft_delete(file_object)

    with tenant_context(tenant.pk):
        file_object.refresh_from_db()
        assert file_object.deleted_at is not None


@pytest.mark.files
def test_reading_a_tenant_scoped_row_from_nowhere_returns_nothing(db, tenant, media):
    """The other half of the rule, asserted so nobody "fixes" it.

    This is not a defect to be worked around by widening a policy — it is the
    protection working. It is stated here because the symptom (DoesNotExist on a
    row you just created) looks like a bug the first three times you meet it.
    """
    file_object = store(tenant_id=tenant.pk, upload=upload(PDF, "invisible.pdf"))

    assert not FileObject.objects.filter(pk=file_object.pk).exists()

    with tenant_context(tenant.pk):
        assert FileObject.objects.filter(pk=file_object.pk).exists()
