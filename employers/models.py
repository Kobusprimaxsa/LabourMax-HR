"""The employer — phase P3.

**``tenant`` and ``employer`` are not the same thing, and the distinction is the
first one to get straight.** A tenant is who subscribes: the account, the billing
relationship, the login. An employer is who employs: the entity whose name appears on
the contract of employment and on the EMP201, and whose sector decides which rule set
every calculator loads.

For a household they are nearly the same, and it is tempting to collapse them. Two
reasons not to. A contract cleaning group can run several employing entities under one
subscription, each with its own PAYE number and its own COIDA registration, and
collapsing them makes that a schema change rather than a second row. And billing is
per tenant while statutory submission is per employer — merging the two means every
future question about "how many employers" is really a question about how many people
are paying, which is a different number.

The three tables here are the ones an employer cannot be onboarded without: who they
are, how they are registered with the revenue and compensation authorities, and where
their salary payments come from.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from core.audit import AuditedModel
from core.db.fields import EncryptedCharField, keyed_hash, last4
from core.models import TenantScopedModel

PROVINCE_CODES = [
    ("EC", "Eastern Cape"),
    ("FS", "Free State"),
    ("GP", "Gauteng"),
    ("KZN", "KwaZulu-Natal"),
    ("LP", "Limpopo"),
    ("MP", "Mpumalanga"),
    ("NC", "Northern Cape"),
    ("NW", "North West"),
    ("WC", "Western Cape"),
]


class Employer(AuditedModel, TenantScopedModel):
    """An entity that employs people and runs payroll.

    ``sector`` is a real foreign key rather than the code string the tenant carries,
    because this is the switch: it decides which leave, working-time and termination
    rule set applies, which minimum wage resolves, and whether the statutory annual
    bonus exists at all. A string would let a typo silently route an employer to the
    BCEA fallback.

    Soft-deleted, never removed. Statutory retention outlasts the customer
    relationship — SARS can ask about a 2026 payroll in 2031, and an employer row
    deleted in 2028 takes every payslip's context with it.
    """

    class EntityType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual or household"
        SOLE_PROPRIETOR = "sole_proprietor", "Sole proprietor"
        COMPANY = "company", "Company"
        CLOSE_CORPORATION = "close_corporation", "Close corporation"
        TRUST = "trust", "Trust"
        PARTNERSHIP = "partnership", "Partnership"
        NON_PROFIT = "non_profit", "Non-profit organisation"

    public_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    trading_name = models.CharField(max_length=150, db_index=True)
    legal_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="As registered. Blank for a household, which has no registered name.",
    )
    entity_type = models.CharField(
        max_length=30, choices=EntityType.choices, default=EntityType.INDIVIDUAL
    )
    registration_number = models.CharField(
        max_length=50, blank=True, help_text="CIPC registration number, where there is one."
    )
    income_tax_reference = models.CharField(max_length=20, blank=True)

    sector = models.ForeignKey(
        "statutory.Sector",
        on_delete=models.PROTECT,
        related_name="employers",
        help_text="Decides every rule set and minimum wage this employer is measured against.",
    )
    sector_area = models.ForeignKey(
        "statutory.SectorArea",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="employers",
        help_text=(
            "Contract cleaning only, and normally derived from the workplace address "
            "rather than asked. NULL for a sector that does not use area rates."
        ),
    )

    # The address is here because the contract cleaning area is derived from it.
    # Asking an employer "are you in Area A or Area C?" gets a confident wrong answer;
    # deriving it from a municipality produces a visible one that can be corrected.
    physical_line1 = models.CharField(max_length=120, blank=True)
    physical_line2 = models.CharField(max_length=120, blank=True)
    physical_suburb = models.CharField(max_length=120, blank=True)
    physical_city = models.CharField(max_length=120, blank=True)
    physical_municipality = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        help_text="What municipality_area_map resolves against.",
    )
    physical_province_code = models.CharField(max_length=10, choices=PROVINCE_CODES, blank=True)
    physical_postal_code = models.CharField(max_length=10, blank=True)

    contact_name = models.CharField(max_length=150, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)

    onboarded_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    deactivated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Soft delete. Statutory retention outlasts the customer relationship.",
    )

    class Meta:
        db_table = "employer"
        ordering = ["trading_name"]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["sector"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "trading_name"], name="uniq_employer_name_per_tenant"
            ),
            models.CheckConstraint(
                condition=models.Q(is_active=True, deactivated_at__isnull=True)
                | models.Q(is_active=False, deactivated_at__isnull=False),
                name="employer_deactivated_at_matches_is_active",
            ),
        ]

    def __str__(self):
        return self.trading_name


class EmployerStatutoryRegistration(AuditedModel, TenantScopedModel):
    """One registration with one authority, for one period.

    Effective-dated because registrations genuinely change: an employer registers for
    PAYE mid-year, changes COIDA class, or joins a bargaining council. A payroll run
    for March must know the reference number as it stood in March, because that is
    what went onto the EMP201.

    **``is_exempt`` is where the SDL exemption lives, and it is a human-set flag.**
    The test is whether the employer reasonably believes their leviable amount over
    the *next* twelve months will exceed the threshold. That cannot be computed from
    payroll history, and any attempt to derive it from last year's figures is wrong —
    which is why this is a boolean somebody ticks, with a reason beside it, and not a
    query.
    """

    class RegistrationType(models.TextChoices):
        PAYE = "paye", "PAYE (SARS)"
        SDL = "sdl", "SDL (SARS)"
        UIF_SARS = "uif_sars", "UIF via SARS"
        UIF_DEL = "uif_del", "UIF via Department of Employment and Labour"
        COIDA = "coida", "COIDA (Compensation Fund)"
        BARGAINING_COUNCIL = "bargaining_council", "Bargaining council"

    employer = models.ForeignKey(
        Employer, on_delete=models.PROTECT, related_name="statutory_registrations"
    )
    registration_type = models.CharField(
        max_length=30, choices=RegistrationType.choices, db_index=True
    )
    reference_number = models.CharField(max_length=40, blank=True)

    registered_from = models.DateField()
    registered_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means currently registered."
    )

    is_exempt = models.BooleanField(
        default=False,
        help_text=(
            "SDL: the employer reasonably believes the next twelve months' leviable "
            "amount will not exceed the threshold. Forward-looking and human-set."
        ),
    )
    exemption_reason = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "employer_statutory_registration"
        ordering = ["employer_id", "registration_type", "-registered_from"]
        indexes = [models.Index(fields=["tenant", "employer", "registration_type"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(registered_to__isnull=True)
                | models.Q(registered_to__gt=models.F("registered_from")),
                name="employer_registration_period_ordered",
            ),
            # One live registration per authority. Many closed ones, at most one open —
            # the same partial-unique shape as a live tenant membership.
            models.UniqueConstraint(
                fields=["employer", "registration_type"],
                condition=models.Q(registered_to__isnull=True),
                name="uniq_live_registration_per_employer_type",
            ),
            models.CheckConstraint(
                condition=models.Q(is_exempt=False) | ~models.Q(exemption_reason=""),
                name="employer_registration_exemption_has_a_reason",
            ),
        ]

    def __str__(self):
        return f"{self.employer_id} {self.registration_type}"


class EmployerBankAccount(AuditedModel, TenantScopedModel):
    """The account salaries are paid from, effective-dated and encrypted.

    Effective-dated for invariant 2: an employer who changes bank in August must
    still reproduce July's payment file exactly, and that file names the old account.

    ``account_number`` is encrypted at rest and **cannot be queried** — see
    ``core/db/fields.py``. The two plain companions carry what the ciphertext cannot:
    ``_last4`` for display, and ``_hash`` for the one equality question that matters,
    "is this the account we already have". Both are maintained on save rather than by
    the caller, because a companion column somebody forgot to set is worse than no
    companion column: the duplicate check then reports that every account is new.
    """

    class AccountType(models.TextChoices):
        CHEQUE = "cheque", "Cheque or current"
        SAVINGS = "savings", "Savings"
        TRANSMISSION = "transmission", "Transmission"

    employer = models.ForeignKey(Employer, on_delete=models.PROTECT, related_name="bank_accounts")
    bank = models.ForeignKey(
        "statutory.Bank", on_delete=models.PROTECT, related_name="employer_accounts"
    )
    branch_code = models.CharField(max_length=10)
    account_holder = models.CharField(max_length=150)
    account_type = models.CharField(
        max_length=20, choices=AccountType.choices, default=AccountType.CHEQUE
    )

    account_number = EncryptedCharField(max_plaintext_length=30)
    account_number_last4 = models.CharField(max_length=4, blank=True, editable=False)
    account_number_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        db_index=True,
        help_text="Keyed HMAC of the normalised number. The only way to ask 'same account?'.",
    )

    is_primary = models.BooleanField(default=True)
    effective_from = models.DateField()
    effective_to = models.DateField(
        null=True, blank=True, help_text="Exclusive. NULL means currently in use."
    )

    class Meta:
        db_table = "employer_bank_account"
        ordering = ["employer_id", "-effective_from"]
        indexes = [models.Index(fields=["tenant", "employer"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="employer_bank_account_period_ordered",
            ),
            # At most one live primary account per employer. A payment file generated
            # against two "primary" accounts picks one by row order.
            models.UniqueConstraint(
                fields=["employer"],
                condition=models.Q(is_primary=True, effective_to__isnull=True),
                name="uniq_live_primary_account_per_employer",
            ),
        ]

    def save(self, *args, **kwargs):
        """Maintain the two companion columns from the encrypted value.

        Done here rather than in a service function because every writer must get it,
        including a data migration and the shell. A row whose hash was never set
        silently defeats the duplicate check it exists for.
        """
        if self.account_number:
            self.account_number_last4 = last4(self.account_number)
            self.account_number_hash = keyed_hash(self.account_number)
        else:
            self.account_number_last4 = ""
            self.account_number_hash = ""

        if kwargs.get("update_fields") is not None:
            update_fields = set(kwargs["update_fields"])
            if "account_number" in update_fields:
                update_fields |= {"account_number_last4", "account_number_hash"}
                kwargs["update_fields"] = sorted(update_fields)

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_from and self.effective_to <= self.effective_from:
            raise ValidationError({"effective_to": "The end date must be after the start date."})

    def __str__(self):
        return f"{self.employer_id} ****{self.account_number_last4}"
