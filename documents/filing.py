"""Filing a document — the one rule that needs a default rather than a refusal.

Everything else about a document is either a column-level CHECK (the exclusive
arc, the paired verified columns) or a cross-row business rule the model's own
``clean()`` can see once the row is built (category-attachment match, sector
match, the expiry requirement, the supersedes cycle). Defaulting
``is_confidential`` from the category is different in kind: it decides what
value a field gets before validation ever runs, and a model field default
cannot tell "the caller left this at False" apart from "the caller chose False
deliberately". This is the one function that has to sit in front of
``clean()`` rather than inside it, mirroring ``employees/engagements.py``.
"""

from __future__ import annotations

from django.db import transaction

from core.managers import tenant_context_of
from documents.models import Document


def file_document(
    *,
    tenant,
    attached_to: str,
    document_category,
    file_object,
    title: str,
    employer=None,
    employee=None,
    workplace=None,
    is_confidential: bool | None = None,
    **fields,
) -> Document:
    """Create and validate a document. Raises ``ValidationError`` and writes nothing
    if the exclusive arc, the category match, the sector match, the expiry
    requirement or the supersedes chain refuses it.

    ``is_confidential`` left as ``None`` takes the category's own
    ``is_confidential_by_default`` (sheet 02); a caller with its own opinion may
    still pass ``True`` or ``False`` explicitly.
    """
    if is_confidential is None:
        is_confidential = document_category.is_confidential_by_default

    document = Document(
        tenant=tenant,
        attached_to=attached_to,
        document_category=document_category,
        file_object=file_object,
        title=title,
        employer=employer,
        employee=employee,
        workplace=workplace,
        is_confidential=is_confidential,
        **fields,
    )

    with transaction.atomic(), tenant_context_of(document):
        document.full_clean()
        document.save()

    return document
