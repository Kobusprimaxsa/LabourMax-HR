"""Document visibility for the two audiences that must never see everything.

Sheet 03: "Employee self-service shows a document only where
document_category.visible_to_employee is TRUE" — a WHITELIST, default deny, so
a category added two years from now is invisible until somebody deliberately
turns the flag on. The read_only staff role asks a different question,
confidentiality rather than audience (sheet 02's ``document.is_confidential``,
defaulted from the category by ``documents/filing.py``).

Nothing outside this module may filter a document queryset for either
audience. A second, slightly different copy of one of these filters — a view
that checks ``is_confidential_by_default`` instead of the row's own
``is_confidential``, say — is exactly how a category added later ends up
visible when nobody decided it should be.

Downloading the bytes behind a visible row goes through ``core/files.py``,
which already gates on the virus scan and writes the ``audit_log`` read row.
Neither is reimplemented here — this module only decides which rows exist to
be downloaded in the first place.
"""

from __future__ import annotations

from core.managers import tenant_context, tenant_context_of
from documents.models import Document


def visible_to_employee(employee) -> list[Document]:
    """This employee's own self-service portal. The whitelist, and nothing else.

    ``document_category.visible_to_employee`` defaults FALSE, so a document
    appears here only where somebody deliberately set it TRUE on the category —
    including a category created after this function was written.
    """
    with tenant_context_of(employee):
        return list(
            Document.objects.filter(
                attached_to=Document.AttachedTo.EMPLOYEE,
                employee=employee,
                document_category__visible_to_employee=True,
            ).order_by("-document_date", "-id")
        )


def visible_to_read_only(tenant_id: int, **filters) -> list[Document]:
    """What the read_only staff role may see: everything except is_confidential.

    A separate question from ``visible_to_employee`` — read_only is a staff
    role with no portal restriction of its own, only a confidentiality one, and
    the two must not be merged into one filter that happens to satisfy both
    today.
    """
    with tenant_context(tenant_id):
        return list(
            Document.objects.filter(**filters)
            .exclude(is_confidential=True)
            .order_by("-document_date", "-id")
        )
