"""Core models — phase P0.

Base classes plus the eleven P0 tables. Every column here traces to sheet 02 of
the Database Specification; if the two disagree, the workbook wins.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from core.audit import AuditedModel
from core.managers import (
    AllTenantsManager,
    TenantOptionalManager,
    TenantScopedManager,
)

# ---------------------------------------------------------------- base classes


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditMixin(TimestampedModel):
    """The four audit columns carried by most tables.

    Populated automatically from the request user. Not repeated in the column
    dictionary — the workbook marks a table 'Audit mixin = Yes' instead.
    """

    created_by_user = models.ForeignKey(
        "core.AppUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )
    updated_by_user = models.ForeignKey(
        "core.AppUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    class Meta:
        abstract = True


class TenantScopedModel(AuditMixin):
    """Every table holding employer or employee data inherits this.

    Carries tenant_id, defaults to the scoped manager, and is picked up
    automatically by the generated isolation test suite. A model that needs
    tenant data and does NOT inherit this will fail CI.
    """

    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="+")

    objects = TenantScopedManager()
    all_tenants = AllTenantsManager()

    class Meta:
        abstract = True


class TenantOptionalModel(models.Model):
    """Security and operations records that span the platform and a tenant.

    An OTP issued before the user has picked a tenant, a login attempt that
    failed before we knew who it was, a platform maintenance job — these have no
    tenant to attribute them to, so ``tenant_id`` is nullable here and NOT NULL
    on ``TenantScopedModel``.

    The nullable column is not a loophole: these tables still carry a row-level
    security policy (``enable_rls_optional``), are still discovered by the
    generated isolation suite, and a tenant session still never sees the
    NULL-tenant rows. See ``core/managers.py`` for the three access rules.

    Deliberately NOT inheriting AuditMixin: these tables are written by the
    system, not by a user editing a record, so created_by/updated_by would be
    two permanently empty columns on the highest-volume tables in the schema.
    """

    tenant = models.ForeignKey(
        "core.Tenant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantOptionalManager()
    all_tenants = AllTenantsManager()

    class Meta:
        abstract = True


# ---------------------------------------------------------------- platform


class PlatformSetting(AuditedModel, AuditMixin):
    """Global, non-tenant configuration."""

    class DataType(models.TextChoices):
        STRING = "string", "String"
        INT = "int", "Integer"
        DECIMAL = "decimal", "Decimal"
        BOOL = "bool", "Boolean"
        JSON = "json", "JSON"
        DATE = "date", "Date"

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(default=dict)
    data_type = models.CharField(max_length=20, choices=DataType.choices, default=DataType.STRING)
    description = models.CharField(max_length=255, blank=True)
    is_secret = models.BooleanField(default=False)

    class Meta:
        db_table = "platform_setting"

    def __str__(self):
        return self.key


class Tenant(AuditedModel, AuditMixin):
    """The account boundary. One tenant = one employer subscription."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        SUSPENDED = "suspended", "Suspended"
        CANCELLED = "cancelled", "Cancelled"
        CLOSED = "closed", "Closed"

    class EntityType(models.TextChoices):
        PRIVATE_HOUSEHOLD = "private_household", "Private household"
        SOLE_PROP = "sole_prop", "Sole proprietor"
        CC = "cc", "Close corporation"
        PTY_LTD = "pty_ltd", "Private company"
        NPO = "npo", "Non-profit"
        TRUST = "trust", "Trust"
        PARTNERSHIP = "partnership", "Partnership"

    class OnboardingStage(models.TextChoices):
        REGISTERED = "registered", "Registered"
        COMPANY_SETUP = "company_setup", "Company setup"
        FIRST_EMPLOYEE = "first_employee", "First employee"
        FIRST_PAYROLL = "first_payroll", "First payroll"
        COMPLETE = "complete", "Complete"

    public_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    trading_name = models.CharField(max_length=150, db_index=True)
    legal_name = models.CharField(max_length=200, blank=True)
    entity_type = models.CharField(
        max_length=30, choices=EntityType.choices, default=EntityType.PRIVATE_HOUSEHOLD
    )
    registration_number = models.CharField(max_length=50, blank=True)
    # primary_sector / sector_area become FKs in P2 when the statutory app lands.
    primary_sector_code = models.CharField(max_length=30, blank=True)
    sector_area_code = models.CharField(max_length=20, blank=True)
    country_code = models.CharField(max_length=2, default="ZA")
    default_timezone = models.CharField(max_length=40, default="Africa/Johannesburg")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    onboarding_stage = models.CharField(
        max_length=30, choices=OnboardingStage.choices, default=OnboardingStage.REGISTERED
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    data_retention_until = models.DateField(
        null=True,
        blank=True,
        help_text="Payroll records retained five years from the last payroll (SARS).",
    )
    max_admin_users = models.SmallIntegerField(
        default=2,
        validators=[MinValueValidator(1)],
        help_text="Business rule: owner plus one additional user. Data, not a constant.",
    )
    locale = models.CharField(max_length=10, default="en-ZA")

    objects = models.Manager()

    class Meta:
        db_table = "tenant"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["trading_name"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_admin_users__gte=1, max_admin_users__lte=5),
                name="tenant_max_admin_users_range",
            ),
        ]

    def __str__(self):
        return self.trading_name


# ---------------------------------------------------------------- identity


class AppUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("user_kind", AppUser.UserKind.PLATFORM_SUPERUSER)
        extra.setdefault("mobile_number", "")
        return self.create_user(email, password, **extra)


class AppUser(AuditedModel, AbstractBaseUser, PermissionsMixin, TimestampedModel):
    """Authentication principal. Not the same thing as an employee.

    An employee is a person on the payroll and usually has no login at all.
    tenant_membership is what joins the two.
    """

    # last_login is written on every sign-in; login_audit already records that
    # properly, so recording it here would bury real changes under noise.
    audit_exclude_fields = ("last_login", "failed_login_count", "last_tenant")
    audit_sensitive_fields = ("password", "mfa_secret")

    class UserKind(models.TextChoices):
        PLATFORM_SUPERUSER = "platform_superuser", "Platform superuser"
        PLATFORM_SUPPORT = "platform_support", "Platform support"
        EMPLOYER = "employer", "Employer"
        EMPLOYEE_SELF_SERVICE = "employee_self_service", "Employee self-service"

    public_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email = models.EmailField(unique=True)
    # Mandatory for every user (decision D-10): carries the OTP and is the
    # fallback when an email address goes dead.
    mobile_number = models.CharField(max_length=20, db_index=True)
    mobile_verified_at = models.DateTimeField(null=True, blank=True)
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    user_kind = models.CharField(
        max_length=30, choices=UserKind.choices, default=UserKind.EMPLOYER, db_index=True
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=255, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    failed_login_count = models.SmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    last_reauth_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Step-up authentication clock. Ownership transfer requires a recent value.",
    )
    last_tenant = models.ForeignKey(
        Tenant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Convenience only — where a multi-tenant user last worked.",
    )
    terms_accepted_version = models.CharField(max_length=20, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    objects = AppUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "app_user"
        indexes = [models.Index(fields=["user_kind"])]

    def __str__(self):
        return self.email


class TenantMembership(AuditedModel, TenantScopedModel):
    """Links a user to a tenant with a role.

    The 'max two admin users' rule is enforced here by trigger and by
    application check, on top of tenant.max_admin_users. Three places, because
    a limit that lives only in a form is not a limit.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        READ_ONLY = "read_only", "Read only"
        EMPLOYEE = "employee", "Employee"

    user = models.ForeignKey(AppUser, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=30, choices=Role.choices, default=Role.ADMIN, db_index=True)
    # employee FK arrives in P4 with the employees app.
    employee_id_ref = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Becomes a real FK to employee in P4. Set only when role='employee'.",
    )
    is_active = models.BooleanField(default=True)
    invited_by_user = models.ForeignKey(
        AppUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Revoked, never deleted — deleting orphans every created_by_user_id.",
    )
    access_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Termination date + 2 months for an employee self-service login (D-08).",
    )

    class Meta:
        db_table = "tenant_membership"
        indexes = [
            models.Index(fields=["tenant", "role"]),
            models.Index(fields=["user"]),
        ]
        constraints = [
            # Partial unique: many revoked rows, at most one live one. This is
            # what lets a role be re-filled after someone leaves (decision D-06).
            models.UniqueConstraint(
                fields=["tenant", "user"],
                condition=models.Q(revoked_at__isnull=True),
                name="uniq_live_membership_per_tenant_user",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} @ tenant {self.tenant_id} ({self.role})"


class UserInvitation(AuditedModel, TenantScopedModel):
    """Outstanding invitation for the second admin or an employee login."""

    email = models.EmailField(db_index=True)
    role = models.CharField(max_length=30, choices=TenantMembership.Role.choices)
    employee_id_ref = models.BigIntegerField(null=True, blank=True)
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 of the emailed token. The raw token is never stored.",
    )
    expires_at = models.DateTimeField(db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    invited_by_user = models.ForeignKey(AppUser, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "user_invitation"
        indexes = [models.Index(fields=["tenant", "email"])]

    def __str__(self):
        return f"{self.email} -> tenant {self.tenant_id}"


class TenantOwnershipTransfer(AuditedModel, TenantScopedModel):
    """Two-sided handover of tenant ownership.

    Both parties re-authenticate. A one-click transfer would be a one-click
    account takeover on an unlocked laptop (decision D-05).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    from_user = models.ForeignKey(AppUser, on_delete=models.PROTECT, related_name="+")
    to_user = models.ForeignKey(AppUser, on_delete=models.PROTECT, related_name="+")
    initiated_at = models.DateTimeField(default=timezone.now)
    initiator_reauth_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True, help_text="72 hours.")
    accepted_at = models.DateTimeField(null=True, blank=True)
    acceptor_reauth_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "tenant_ownership_transfer"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_user=models.F("to_user")),
                name="ownership_transfer_distinct_parties",
            ),
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(status="pending"),
                name="uniq_pending_ownership_transfer_per_tenant",
            ),
        ]

    def __str__(self):
        return f"transfer {self.from_user_id} -> {self.to_user_id} ({self.status})"


class OtpChallenge(TenantOptionalModel, TimestampedModel):
    """One-time passcodes by SMS.

    Deliberately one table for login, document access, step-up auth and mobile
    verification, so throttling and lockout are enforced in one place.
    """

    class Purpose(models.TextChoices):
        LOGIN = "login", "Login"
        DOCUMENT_ACCESS = "document_access", "Document access"
        STEP_UP = "step_up", "Step-up authentication"
        MOBILE_VERIFICATION = "mobile_verification", "Mobile verification"

    purpose = models.CharField(max_length=30, choices=Purpose.choices, db_index=True)
    user = models.ForeignKey(
        AppUser, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    employee_id_ref = models.BigIntegerField(null=True, blank=True)
    mobile_number = models.CharField(max_length=20, db_index=True)
    code_hash = models.CharField(max_length=64)
    issued_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True, help_text="Five minutes.")
    attempts = models.SmallIntegerField(default=0, help_text="Locked after five.")
    verified_at = models.DateTimeField(null=True, blank=True)
    device_token_hash = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="Trusts the device for 30 days so a payslip view does not cost an SMS (D-13).",
    )
    device_trusted_until = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "otp_challenge"
        indexes = [models.Index(fields=["mobile_number", "-issued_at"])]

    def __str__(self):
        return f"{self.purpose} otp for {self.mobile_number}"


# ---------------------------------------------------------------- audit & files


class LoginAudit(TenantOptionalModel):
    """Append-only record of every authentication attempt."""

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        BAD_PASSWORD = "bad_password", "Bad password"
        UNKNOWN_USER = "unknown_user", "Unknown user"
        LOCKED = "locked", "Locked"
        MFA_FAILED = "mfa_failed", "MFA failed"
        MFA_SUCCESS = "mfa_success", "MFA success"

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(
        AppUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    email_attempted = models.EmailField(blank=True, db_index=True)
    outcome = models.CharField(max_length=30, choices=Outcome.choices, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    session_key_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "login_audit"
        indexes = [models.Index(fields=["user", "-occurred_at"])]

    def __str__(self):
        return f"{self.outcome} {self.email_attempted} {self.occurred_at:%Y-%m-%d %H:%M}"


class AuditLog(TenantOptionalModel):
    """Field-level change history. Append-only, partitioned monthly at volume.

    The POPIA and SARS answer to 'who changed this figure, when, and what was
    it before'. The application role holds no UPDATE or DELETE grant.
    """

    class Operation(models.TextChoices):
        INSERT = "insert", "Insert"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"

    class ActorKind(models.TextChoices):
        USER = "user", "User"
        SYSTEM = "system", "System"
        SUPPORT = "support", "Support"
        API = "api", "API"

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_user = models.ForeignKey(
        AppUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_kind = models.CharField(max_length=20, choices=ActorKind.choices, default=ActorKind.USER)
    impersonated_by_user = models.ForeignKey(
        AppUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Set when platform support acts on a tenant's behalf.",
    )
    table_name = models.CharField(max_length=64, db_index=True)
    record_pk = models.BigIntegerField(db_index=True)
    operation = models.CharField(max_length=10, choices=Operation.choices)
    changed_fields = models.JSONField(
        default=dict,
        help_text="{'field': {'old': x, 'new': y}}. Sensitive fields stored as masked markers.",
    )
    business_event = models.CharField(max_length=60, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(fields=["tenant", "-occurred_at"]),
            models.Index(fields=["table_name", "record_pk", "-occurred_at"]),
            models.Index(fields=["business_event", "-occurred_at"]),
        ]

    def __str__(self):
        return f"{self.operation} {self.table_name}#{self.record_pk}"


class FileObject(AuditedModel, TenantScopedModel):
    """Single abstraction over every stored file.

    Nothing writes bytes anywhere else, which keeps retention, virus scanning
    and deletion in one place.
    """

    # storage_key is derived, and checksum changing means the bytes changed,
    # which scan_status and size_bytes already tell the reader.
    audit_exclude_fields = ("storage_key", "checksum_sha256")

    class ScanStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"
        FAILED = "failed", "Failed"

    public_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    storage_backend = models.CharField(max_length=20, default="s3")
    storage_key = models.CharField(max_length=500, unique=True)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField(default=0)
    checksum_sha256 = models.CharField(max_length=64, db_index=True)
    scan_status = models.CharField(
        max_length=20,
        choices=ScanStatus.choices,
        default=ScanStatus.PENDING,
        db_index=True,
        help_text="Downloads are blocked until clean.",
    )
    scanned_at = models.DateTimeField(null=True, blank=True)
    is_encrypted = models.BooleanField(default=True)
    retention_until = models.DateField(null=True, blank=True, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "file_object"
        indexes = [models.Index(fields=["tenant", "scan_status"])]

    def __str__(self):
        return self.original_filename


class BackgroundJob(TenantOptionalModel):
    """Visibility over queued and scheduled work, without reading broker internals."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        RETRYING = "retrying", "Retrying"
        CANCELLED = "cancelled", "Cancelled"

    job_name = models.CharField(max_length=80, db_index=True)
    task_id = models.CharField(max_length=120, unique=True, null=True, blank=True)
    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    parameters = models.JSONField(default=dict)
    result_summary = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.SmallIntegerField(default=0)
    triggered_by_user = models.ForeignKey(
        AppUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "background_job"
        indexes = [models.Index(fields=["status", "queued_at"])]

    def __str__(self):
        return f"{self.job_name} ({self.status})"
