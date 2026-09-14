"""Leave — phase P6, domain 6. ``leave_type`` arrives early, in P4.

Only ``leave_type`` is here. The rest of the domain — cycles, the transaction
ledger, applications, the accrual engine — is P6 and is listed in
``docs/PHASES.md``.

**Why this one table is out of phase.** ``employee_leave_entitlement`` is a P4
table and it is meaningless without something to point at: it records that *this
employee* gets more than the statute allows, or a different accrual method, for
*this kind of leave*. The alternatives were both worse than pulling one table
forward. Leaving the entitlement table out would have closed P4 with a hole in the
employee master file, and P6 would then have to revisit P4's migrations. Pointing
the FK at a string code would have put an unenforceable reference in the schema,
which is the failure ``PROTECT`` exists to prevent (D-127).

**A NULL tenant means shared, not orphaned** — the same shape as
``payroll_component`` (D-87). The platform stocks the statutory types and an
employer adds its own on top: a birthday day, a long-service day, study leave on
terms better than the BCEA's. A tenant reads its own rows and the platform's
together and writes only its own.

**No entitlement figure lives here.** The number of days is a *statutory* figure
and belongs in ``leave_rule_set`` with its citation, which is where P2 already put
it. What this table carries is the *shape* of a leave type: whether it accrues,
whether it is paid, whether it survives termination, how long its cycle runs.
``cycle_months`` is the one that looks like a rate and is not — 12 for annual, 36
for sick — because it is a property of the cycle rather than an amount of
anything, and the days that accrue inside it are still read from the rule set.

**``balance_source`` is what makes sub-types work.** Annual leave taken without
authorisation still consumes the annual balance, so ``ANNUAL_UNAUTHORISED`` points
at ``ANNUAL`` as its parent and draws on the parent's balance rather than keeping
its own. Without it, an employer recording unauthorised absence would hand the
employee back the day.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from core.audit import AuditedModel
from core.models import TenantSharedModel


class LeaveType(AuditedModel, TenantSharedModel):
    """A kind of leave, and how it behaves. Not how much of it there is."""

    class Code(models.TextChoices):
        ANNUAL = "ANNUAL", "Annual leave"
        ANNUAL_UNAUTHORISED = "ANNUAL_UNAUTHORISED", "Annual leave — unauthorised"
        SICK = "SICK", "Sick leave"
        FAMILY_RESPONSIBILITY = "FAMILY_RESPONSIBILITY", "Family responsibility leave"
        MATERNITY = "MATERNITY", "Maternity leave"
        PARENTAL = "PARENTAL", "Parental leave"
        ADOPTION = "ADOPTION", "Adoption leave"
        UNPAID = "UNPAID", "Unpaid leave"
        STUDY = "STUDY", "Study leave"
        COMPASSIONATE = "COMPASSIONATE", "Compassionate leave"

    class BalanceSource(models.TextChoices):
        OWN = "own", "Keeps its own balance"
        PARENT = "parent", "Consumes the parent type's balance"
        NONE = "none", "Has no balance at all"

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)

    parent_leave_type = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sub_types",
        help_text="Lets a sub-type draw on a parent balance.",
    )
    balance_source = models.CharField(
        max_length=30,
        choices=BalanceSource.choices,
        default=BalanceSource.OWN,
    )

    is_paid = models.BooleanField(default=True)
    is_statutory = models.BooleanField(default=True)
    accrues = models.BooleanField(
        default=True, help_text="FALSE for unpaid and unauthorised leave."
    )
    cycle_months = models.SmallIntegerField(
        default=12, help_text="12 for annual and family responsibility, 36 for sick."
    )
    requires_evidence = models.BooleanField(default=False)
    payable_on_termination = models.BooleanField(
        default=False, help_text="TRUE only for annual leave — BCEA s40(b)."
    )
    reduces_pay_when_exhausted = models.BooleanField(
        default=True, help_text="Converts to unpaid once the balance is zero."
    )
    counts_as_service = models.BooleanField(
        default=True, help_text="Whether the period counts toward service length."
    )

    # D-127 said this column exists and the migration installs the lock that keys
    # on it; the column itself was never written, so every UPDATE and DELETE on
    # ANY leave_type row — a tenant's own included — failed inside the trigger with
    # `record "old" has no field "is_system"`. Migration 0002 adds it. The one test
    # that covered the area asserted `DatabaseError` and passed on the wrong error,
    # which is the argument for asserting on the message as well as the class.
    is_system = models.BooleanField(
        default=False, help_text="System types cannot be edited or deleted (D-93)."
    )

    colour_hex = models.CharField(max_length=7, default="#4A7C9E")
    display_order = models.SmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "leave_type"
        ordering = ["display_order", "code"]
        constraints = [
            # A shared row IS a system row, and a tenant's row is never one — the
            # same pair payroll_component carries, and for the same reason. Without
            # the first, a shared row with is_system false is readable by every
            # tenant and deletable by any of them, because a DELETE is checked
            # against the policy's USING clause only (D-93). Without the second, an
            # employer could mint a row of its own that it can then never edit.
            models.CheckConstraint(
                condition=models.Q(tenant__isnull=False) | models.Q(is_system=True),
                name="leave_type_shared_rows_are_system_rows",
            ),
            models.CheckConstraint(
                condition=models.Q(tenant__isnull=True) | models.Q(is_system=False),
                name="leave_type_system_rows_are_shared_rows",
            ),
            # Coalesce, because NULL = NULL is unknown in PostgreSQL and the shared
            # rows are exactly the ones with a NULL tenant — so a plain unique would
            # permit the platform to stock ANNUAL twice.
            models.UniqueConstraint(
                models.functions.Coalesce("tenant_id", models.Value(0)),
                "code",
                name="uniq_leave_type_code_per_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(balance_source__in=["own", "parent", "none"]),
                name="leave_type_balance_source_is_known",
            ),
            models.CheckConstraint(
                condition=models.Q(cycle_months__gt=0),
                name="leave_type_cycle_is_positive",
            ),
            # The colour lands in a leave calendar, a payslip legend and a PDF. A
            # value that is not a hex triplet renders as whatever the browser
            # guesses in one place and as black in the PDF, which reads as a bug in
            # the document rather than as a bad setting.
            models.CheckConstraint(
                condition=models.Q(colour_hex__regex=r"^#[0-9A-Fa-f]{6}$"),
                name="leave_type_colour_is_a_hex_triplet",
            ),
            # A type that draws on a parent must have one, and a type with no parent
            # cannot draw on one. Either half alone leaves a balance nobody can find.
            models.CheckConstraint(
                condition=~models.Q(balance_source="parent")
                | models.Q(parent_leave_type__isnull=False),
                name="leave_type_parent_balance_needs_a_parent",
            ),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"

    def clean(self):
        super().clean()

        if self.parent_leave_type_id == self.pk and self.pk is not None:
            raise ValidationError({"parent_leave_type": "A leave type cannot be its own parent."})

        # One level only. A grandparent chain means resolving a balance walks an
        # arbitrary number of rows, and every accrual read pays for it.
        if self.parent_leave_type_id is not None:
            parent = self.parent_leave_type
            if parent.parent_leave_type_id is not None:
                raise ValidationError(
                    {
                        "parent_leave_type": (
                            f"{parent.code} already draws on another type. Sub-types are "
                            "one level deep, so point this at the type that owns the "
                            "balance."
                        )
                    }
                )

        # A shared type pointing at a tenant's row would put one employer's private
        # leave type in the catalogue every other employer reads — through the FK,
        # where no policy is looking. PROTECT then stops that tenant deleting their
        # own row, and the reason would be invisible to them.
        if self.tenant_id is None and self.parent_leave_type_id is not None:
            if self.parent_leave_type.tenant_id is not None:
                raise ValidationError(
                    {
                        "parent_leave_type": (
                            "A shared leave type cannot draw on one employer's own "
                            "type. Stock the parent in the catalogue first."
                        )
                    }
                )

        if self.balance_source == self.BalanceSource.NONE and self.accrues:
            raise ValidationError(
                {
                    "accrues": (
                        "A type with no balance cannot accrue into one. Unpaid and "
                        "unauthorised leave are recorded, not accrued."
                    )
                }
            )

        if self.payable_on_termination and not self.is_paid:
            raise ValidationError(
                {
                    "payable_on_termination": (
                        "Unpaid leave cannot be paid out on termination — there is no "
                        "rate to pay it at."
                    )
                }
            )
