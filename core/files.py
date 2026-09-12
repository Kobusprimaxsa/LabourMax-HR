"""The one place bytes are written, and the one place a download is allowed.

Nothing else in the system touches storage. That is the whole design: retention,
virus scanning, quota and deletion each have exactly one place to live, and a new
feature in P9 that serves a generated contract cannot accidentally skip the scan
gate because there is no other way to get a file out.

The rules, each here because of a specific way file handling goes wrong:

**Downloads are blocked until the scan says clean.** One function decides, and it
refuses by default — a file in any state other than ``clean`` is not served. An
``infected`` file is never served and never quietly re-scanned into clean.

**Every download writes an audit row.** A subject access request under POPIA asks
who looked at a document, not just who changed it. Reads are the interesting
question for documents, which is why ``AuditLog.Operation`` has a ``read`` member.

**The storage key never derives from the uploaded filename.** Two reasons. A
filename is attacker-controlled, so building a path from it is a traversal bug
waiting to happen. And filenames carry personal information — ``Thabo_Mokoena_ID.pdf``
would end up in bucket listings, CDN logs and error reports, which is a POPIA
disclosure nobody intended. The original name is a database column; the key is a
UUID.

**Content type comes from the bytes, not from the client.** The browser's
``Content-Type`` header is a claim, not evidence.

**The size cap is checked before anything is written.** Checking afterwards means
a 2GB upload has already cost the bandwidth and the disk.
"""

from __future__ import annotations

import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

DEFAULT_MAX_BYTES = 20 * 1024 * 1024

# Magic-byte signatures for the types an HR system legitimately receives:
# identity documents, CVs, medical certificates, CIPC registrations, photographs.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# ZIP-based Office formats share one signature, so the declared extension is the
# only way to tell them apart. An unrecognised extension on a ZIP is refused
# rather than guessed: a .zip of anything is not something to store and serve.
_ZIP_SIGNATURE = b"PK\x03\x04"
_ZIP_EXTENSIONS = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

ALLOWED_CONTENT_TYPES = frozenset(
    {content_type for _, content_type in _SIGNATURES}
    | set(_ZIP_EXTENSIONS.values())
    | {"text/plain", "text/csv"}
)

_TEXT_EXTENSIONS = {"txt": "text/plain", "csv": "text/csv"}


class FileRejectedError(ValidationError):
    """The upload was refused. The message is safe to show a user."""


class DownloadRefusedError(ValidationError):
    """The file exists but must not be served. Never leaks why beyond the status."""


def max_upload_bytes() -> int:
    return getattr(settings, "FILE_UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES)


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def sniff_content_type(head: bytes, filename: str) -> str | None:
    """The real type of these bytes, or None if it is not a type we accept.

    Deliberately narrow. A sniffer that recognises everything is a sniffer that
    accepts a polyglot file, and the point of this function is to refuse.
    """
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type

    if head.startswith(_ZIP_SIGNATURE):
        return _ZIP_EXTENSIONS.get(extension_of(filename))

    extension = extension_of(filename)
    if extension in _TEXT_EXTENSIONS:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return _TEXT_EXTENSIONS[extension]

    return None


def storage_key_for(tenant_id: int) -> str:
    """Unguessable, tenant-prefixed, and carrying nothing about the file.

    The tenant prefix is for lifecycle rules and for making a stray object
    attributable — not for access control. Access control is the database row.
    """
    return f"tenant/{tenant_id}/{uuid.uuid4().hex}"


def store(
    *,
    tenant_id: int,
    upload,
    original_filename: str | None = None,
    retention_until=None,
    is_encrypted: bool = True,
) -> object:
    """Validate, write the bytes, and record the row. The only way in.

    ``upload`` is any file-like object with ``read()``; a Django
    ``UploadedFile`` works, and so does ``BytesIO`` in tests.

    Returns a ``FileObject`` with ``scan_status='pending'``. It is not
    downloadable until something calls ``record_scan_result``.
    """
    from core.models import FileObject

    name = original_filename or getattr(upload, "name", "") or "unnamed"

    data = upload.read()
    size = len(data)
    if size == 0:
        raise FileRejectedError("The file is empty.")
    limit = max_upload_bytes()
    if size > limit:
        raise FileRejectedError(
            f"The file is {size // 1024 // 1024} MB. The maximum is {limit // 1024 // 1024} MB."
        )

    content_type = sniff_content_type(data[:512], name)
    if content_type is None or content_type not in ALLOWED_CONTENT_TYPES:
        raise FileRejectedError(
            "That file type is not accepted. Allowed: PDF, PNG, JPEG, GIF, Word, "
            "Excel, PowerPoint, plain text and CSV."
        )

    key = storage_key_for(tenant_id)
    checksum = hashlib.sha256(data).hexdigest()

    stored_key = default_storage.save(key, ContentFile(data))

    from core.managers import tenant_context

    with tenant_context(tenant_id):
        return FileObject.objects.create(
            tenant_id=tenant_id,
            storage_backend=type(default_storage).__name__,
            storage_key=stored_key,
            original_filename=name[:255],
            content_type=content_type,
            size_bytes=size,
            checksum_sha256=checksum,
            scan_status=FileObject.ScanStatus.PENDING,
            is_encrypted=is_encrypted,
            retention_until=retention_until,
        )


def record_scan_result(file_object, status: str, *, scanned_at=None):
    """Record what the scanner found. The only way a file becomes downloadable.

    An ``infected`` verdict is terminal: it is never overwritten, because the
    realistic way that happens is a re-scan against a broken definitions file,
    and the cost of wrongly serving malware to an employer is not symmetric with
    the cost of asking them to upload again.
    """
    from core.models import FileObject

    if status not in FileObject.ScanStatus.values:
        raise ValueError(f"Unknown scan status: {status}")

    if file_object.scan_status == FileObject.ScanStatus.INFECTED:
        raise FileRejectedError("An infected file cannot be re-scanned. Upload a clean copy.")

    from core.managers import tenant_context_of

    file_object.scan_status = status
    file_object.scanned_at = scanned_at or timezone.now()
    with tenant_context_of(file_object):
        file_object.save(update_fields=["scan_status", "scanned_at", "updated_at"])
    return file_object


def assert_downloadable(file_object) -> None:
    """Refuse by default. Anything not explicitly clean and live is not served."""
    from core.models import FileObject

    if file_object.deleted_at is not None:
        raise DownloadRefusedError("That file is no longer available.")

    if file_object.scan_status != FileObject.ScanStatus.CLEAN:
        if file_object.scan_status == FileObject.ScanStatus.INFECTED:
            raise DownloadRefusedError("That file failed a virus scan and cannot be downloaded.")
        raise DownloadRefusedError("That file is still being checked. Try again shortly.")


def open_for_download(file_object, *, business_event: str = "file_downloaded"):
    """Return an open file handle, and record that it was read.

    The audit row is written BEFORE the handle is returned. If the write fails
    the download fails, which is the correct order for a POPIA access log: an
    unrecorded disclosure is worse than a failed one.
    """
    from core.audit import _write_read_event
    from core.managers import tenant_context_of

    assert_downloadable(file_object)
    with tenant_context_of(file_object):
        _write_read_event(file_object, business_event=business_event)
    return default_storage.open(file_object.storage_key, "rb")


def soft_delete(file_object, *, at=None):
    """Mark the row deleted. The bytes stay until retention allows a purge.

    Statutory retention outlasts the customer relationship — payroll records are
    kept five years from the last payroll — so a user pressing delete must not be
    able to destroy evidence the employer is legally required to hold.
    """
    from core.managers import tenant_context_of

    file_object.deleted_at = at or timezone.now()
    with tenant_context_of(file_object):
        file_object.save(update_fields=["deleted_at", "updated_at"])
    return file_object
