"""Document filing — phase P4, domain 11.

Two tables. ``document_category`` is the catalogue of what may be filed and the
rules that go with each kind; ``document`` is a filed instance, attached to
exactly one of a tenant, an employer, an employee or a workplace.

**``document_category`` is the THIRD shared table** (D-138), a NULL ``tenant_id``
meaning "system-seeded, available to every tenant" — the same shape as
``payroll_component`` (D-87) and ``leave_type`` (D-127). Sheet 02 does not give it
an ``is_system`` column, and it needs one anyway, for D-134's exact reason:
``lock_system_rows()`` keys on ``is_system``, and installing that trigger on a
table without the column makes every UPDATE and DELETE on the table fail inside
it — a tenant renaming its own category included. Unlike ``leave_type``, this
column is present from the first migration rather than retrofitted, so the bug
D-134 fixed never has a chance to exist here.

**``document`` carries an EXCLUSIVE ARC that is asymmetric on purpose.** An
employee belongs to an employer and a workplace belongs to an employer, so an
employee- or workplace-attached document may ALSO name the employer — the last
two arms of the CHECK deliberately leave ``employer_id`` free rather than
forcing it null. Tidying that into symmetry would refuse a perfectly normal
attachment: a police clearance filed against an employee, with the employer it
was gathered for still on the row.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Value
from django.db.models.functions import Coalesce

from core.audit import AuditedModel
from core.models import TenantScopedModel, TenantSharedModel


class DocumentCategory(AuditedModel, TenantSharedModel):
    """A kind of document, and the filing rules that go with it (D-43).

    A table rather than an enum so a tenant can add its own category — a
    household's own house-rules acknowledgement, say — without a release, while
    the platform stocks the categories every employer needs (D-138).
    """

    class AppliesTo(models.TextChoices):
        TENANT = "tenant", "Tenant"
        EMPLOYER = "employer", "Employer"
        EMPLOYEE = "employee", "Employee"
        WORKPLACE = "workplace", "Workplace"
        ANY = "any", "Any"

    code = models.CharField(max_length=50)
    name = models.CharField(max_length=120)
    applies_to = models.CharField(
        max_length=20,
        choices=AppliesTo.choices,
        default=AppliesTo.EMPLOYEE,
        help_text="What a document in this category may attach to. 'any' fits all four.",
    )
    sector = models.ForeignKey(
        "statutory.Sector",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="document_categories",
        help_text=(
            "NULL fits every sector. Set only where the category is meaningless "
            "outside one — CIPC and BEE are not household documents."
        ),
    )
    requires_expiry_date = models.BooleanField(
        default=False, help_text="Work permits, police clearances, COIDA letters of good standing."
    )
    is_required_for_onboarding = models.BooleanField(
        default=False, help_text="Feeds the outstanding-documents count on the employee file."
    )
    visible_to_employee = models.BooleanField(
        default=False,
        help_text=(
            "WHITELIST, default deny. TRUE only for the employment contract. A "
            "category added later is invisible until someone deliberately opens it."
        ),
    )
    is_confidential_by_default = models.BooleanField(
        default=False,
        help_text="Hides from the read_only role. Separate question from D-44's whitelist.",
    )
    default_retention_months = models.SmallIntegerField(
        null=True, blank=True, help_text="Seeds file_object.retention_until on upload."
    )
    sort_order = models.SmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    # D-138: the workbook has no column for this, and D-134 is the reason it
    # cannot be left out. lock_system_rows() below keys on it; without the
    # column the trigger raises `record "old" has no field "is_system"` on
    # every UPDATE and DELETE on the table, a tenant editing its own row
    # included. It ships WITH the column from this table's first migration, so
    # the bug D-134 fixed never has a window to exist here.
    is_system = models.BooleanField(
        default=False, help_text="System categories cannot be edited or deleted (D-93)."
    )

    class Meta:
        db_table = "document_category"
        ordering = ["sort_order", "code"]
        indexes = [models.Index(fields=["applies_to", "is_active"])]
        constraints = [
            # Coalesce, because NULL = NULL is unknown in PostgreSQL and every
            # shared row has a NULL tenant — a plain unique would let the
            # platform stock ID_COPY twice.
            models.UniqueConstraint(
                Coalesce("tenant_id", Value(0)),
                "code",
                name="uniq_document_category_code_per_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    applies_to__in=["tenant", "employer", "employee", "workplace", "any"]
                ),
                name="document_category_applies_to_is_known",
            ),
            # The pair payroll_component and leave_type both carry, for the same
            # reason: without the first, a shared row with is_system false is
            # readable by every tenant and deletable by any of them, because a
            # DELETE is checked against the policy's USING clause only (D-93).
            # Without the second, a tenant could mint a row it can never edit.
            models.CheckConstraint(
                condition=models.Q(tenant__isnull=False) | models.Q(is_system=True),
                name="document_category_shared_rows_are_system_rows",
            ),
            models.CheckConstraint(
                condition=models.Q(tenant__isnull=True) | models.Q(is_system=False),
                name="document_category_system_rows_are_shared_rows",
            ),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class Document(AuditedModel, TenantScopedModel):
    """One filed document. ``tenant_id`` is the RLS scope, not the attachment point.

    ``attached_to`` plus three nullable FKs form an exclusive arc rather than a
    generic ``owner_type``/``owner_id`` pair, because a generic pair produces
    orphans the first time a referenced row is deleted — nothing in the schema
    stops it. Four real foreign keys give real referential integrity (D-41).
    """

    #: A renewal chain (2024 permit -> 2026 -> 2028) is legitimate; a cycle is
    #: a data error. This bounds how far clean() walks before concluding the
    #: chain is broken rather than merely long.
    MAX_SUPERSEDES_DEPTH = 50

    class AttachedTo(models.TextChoices):
        TENANT = "tenant", "Tenant"
        EMPLOYER = "employer", "Employer"
        EMPLOYEE = "employee", "Employee"
        WORKPLACE = "workplace", "Workplace"

    attached_to = models.CharField(max_length=20, choices=AttachedTo.choices, db_index=True)

    employer = models.ForeignKey(
        "employers.Employer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text=(
            "Set when attached_to='employer', and also when 'employee' or 'workplace' name one."
        ),
    )
    employee = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text="Set only when attached_to='employee'.",
    )
    workplace = models.ForeignKey(
        "employers.Workplace",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text=(
            "Set only when attached_to='workplace' — client contracts, site "
            "instructions, safety files."
        ),
    )
    document_category = models.ForeignKey(
        DocumentCategory, on_delete=models.PROTECT, related_name="documents"
    )

    title = models.CharField(max_length=200)
    description = models.CharField(max_length=500, blank=True)
    file_object = models.ForeignKey(
        "core.FileObject",
        on_delete=models.PROTECT,
        related_name="documents",
        help_text="Carries the virus scan status, checksum and retention date.",
    )

    reference_number = models.CharField(
        max_length=60,
        blank=True,
        help_text="Permit number, CIPC registration number, certificate number.",
    )
    document_date = models.DateField(
        null=True, blank=True, help_text="Date on the document itself."
    )
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(
        null=True, blank=True, help_text="Drives alerts at 60, 30 and 7 days (P9)."
    )

    is_confidential = models.BooleanField(
        default=False, help_text="Defaulted from the category; hides from the read_only role."
    )

    verified_by_user = models.ForeignKey(
        "core.AppUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Someone confirmed this is what it claims to be.",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    supersedes_document = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by",
        help_text="A renewed permit supersedes rather than replaces the old one.",
    )

    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = "document"
        ordering = ["-document_date", "-id"]
        indexes = [
            models.Index(fields=["tenant", "attached_to"]),
            models.Index(
                fields=["employee", "document_category"],
                condition=models.Q(employee__isnull=False),
                name="document_employee_category_idx",
            ),
            models.Index(
                fields=["employer"],
                condition=models.Q(employer__isnull=False),
                name="document_employer_idx",
            ),
            models.Index(
                fields=["workplace"],
                condition=models.Q(workplace__isnull=False),
                name="document_workplace_idx",
            ),
            models.Index(
                fields=["tenant", "expiry_date"],
                condition=models.Q(expiry_date__isnull=False),
                name="document_tenant_expiry_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attached_to__in=["tenant", "employer", "employee", "workplace"]),
                name="document_attached_to_is_known",
            ),
            # THE EXCLUSIVE ARC IS ASYMMETRIC ON PURPOSE. Implemented verbatim
            # from sheet 03 — do not tidy it into symmetry. The last two arms
            # deliberately leave employer_id free: an employee belongs to an
            # employer and a workplace belongs to an employer, so the document
            # may name both.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attached_to="tenant",
                        employer__isnull=True,
                        employee__isnull=True,
                        workplace__isnull=True,
                    )
                    | models.Q(
                        attached_to="employer",
                        employer__isnull=False,
                        employee__isnull=True,
                        workplace__isnull=True,
                    )
                    | models.Q(
                        attached_to="employee", employee__isnull=False, workplace__isnull=True
                    )
                    | models.Q(
                        attached_to="workplace", workplace__isnull=False, employee__isnull=True
                    )
                ),
                name="document_exclusive_arc",
            ),
            # Mirrors reference_data_version's verified_by_user/verified_at pair
            # (statutory/models.py): set together, or not at all.
            models.CheckConstraint(
                condition=models.Q(verified_at__isnull=True, verified_by_user__isnull=True)
                | models.Q(verified_at__isnull=False, verified_by_user__isnull=False),
                name="document_verified_by_and_at_together",
            ),
        ]

    def __str__(self):
        return self.title

    def _resolves_employer(self):
        """The employer this document's attachment implies, or None for 'tenant'.

        A tenant may run several employing entities (a contract cleaning group,
        each with its own PAYE number), so a 'tenant'-attached document has no
        single employer to test a category's sector restriction against — the
        check is skipped rather than guessed.
        """
        if self.attached_to == self.AttachedTo.EMPLOYER:
            return self.employer
        if self.attached_to == self.AttachedTo.EMPLOYEE:
            return self.employee.employer if self.employee_id else None
        if self.attached_to == self.AttachedTo.WORKPLACE:
            return self.workplace.employer if self.workplace_id else None
        return None

    def clean(self):
        super().clean()

        category = self.document_category if self.document_category_id else None
        if category is not None:
            if (
                category.applies_to != DocumentCategory.AppliesTo.ANY
                and category.applies_to != self.attached_to
            ):
                raise ValidationError(
                    {
                        "document_category": (
                            f"{category.code} applies to {category.get_applies_to_display()}, "
                            f"not to a document attached to {self.attached_to}."
                        )
                    }
                )

            if category.requires_expiry_date and self.expiry_date is None:
                raise ValidationError(
                    {
                        "expiry_date": (
                            f"{category.code} requires an expiry date — it is one of the "
                            f"categories a payroll run needs to know is about to lapse."
                        )
                    }
                )

            employer = self._resolves_employer()
            if category.sector_id is not None and employer is not None:
                if employer.sector_id != category.sector_id:
                    raise ValidationError(
                        {
                            "document_category": (
                                f"{category.code} is only for the {category.sector.name} "
                                f"sector, and {employer} is not in it. A household must not "
                                f"be offered a contract-cleaning-only category."
                            )
                        }
                    )

        if self.supersedes_document_id is not None:
            if self.pk is not None and self.supersedes_document_id == self.pk:
                raise ValidationError(
                    {"supersedes_document": "A document cannot supersede itself."}
                )

            # Walk the chain looking for a cycle. A renewal chain (2024 permit ->
            # 2026 -> 2028) is legitimate and ends at None; a cycle revisits a
            # row already walked, including this one.
            seen = {self.pk} if self.pk is not None else set()
            current_id = self.supersedes_document_id
            for _ in range(self.MAX_SUPERSEDES_DEPTH):
                if current_id is None:
                    break
                if current_id in seen:
                    raise ValidationError(
                        {
                            "supersedes_document": (
                                "This would create a cycle in the renewal chain — a "
                                "document eventually supersedes itself through others."
                            )
                        }
                    )
                seen.add(current_id)
                current_id = (
                    Document.objects.filter(pk=current_id)
                    .values_list("supersedes_document_id", flat=True)
                    .first()
                )
            else:
                if current_id is not None:
                    raise ValidationError(
                        {
                            "supersedes_document": (
                                f"Renewal chain is more than {self.MAX_SUPERSEDES_DEPTH} "
                                f"documents deep. Refused rather than assumed to be a cycle "
                                f"that has not looped back yet."
                            )
                        }
                    )
