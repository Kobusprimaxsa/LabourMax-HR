"""Field-level change history — the POPIA and SARS answer to "who changed this".

Three questions this module has to answer for a row eighteen months old: who
changed it, when, and what was it before. Nothing else in the system records the
"before".

How it works
------------
A ``post_init`` receiver snapshots the values a row was loaded with. ``post_save``
diffs the snapshot against what was written. That costs a dict per loaded
instance and **no extra queries**, which is why it is preferred over re-reading
the row in ``pre_save``: an audit trail that doubles the query count on every
payroll run gets switched off by the first person who profiles it.

What gets audited
-----------------
Only models inheriting ``AuditedModel``. Discovery is by ``issubclass``, exactly
like tenant isolation, so a misspelled marker is an import error rather than a
silently unaudited table.

Deliberately NOT audited: ``audit_log`` itself (recursion), ``login_audit`` and
``otp_challenge`` (their own records, high volume, full of secrets), and
``background_job`` (machine chatter, not human change).

Sensitive values are never stored
---------------------------------
For a sensitive field the row records *that it changed*, never the values. There
is no point protecting an ID number in ``employee`` and then writing it in clear
into a table designed to be kept for years. Per-model declarations are additive
to ``SENSITIVE_NAME_FRAGMENTS`` below, which is the safety net for the field
someone forgets to declare.

Failure policy
--------------
An audit write that fails takes the transaction down with it. A compliance log
that silently drops entries under load is worse than no log, because you cannot
tell the difference between "nothing happened" and "the recorder was broken".
"""

from __future__ import annotations

import contextvars
import datetime
import uuid
from decimal import Decimal

from django.db import models

# --------------------------------------------------------------------- context

_actor_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "audit_actor_user_id", default=None
)
_actor_kind: contextvars.ContextVar[str] = contextvars.ContextVar(
    "audit_actor_kind", default="system"
)
_impersonated_by_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "audit_impersonated_by_user_id", default=None
)
_ip_address: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "audit_ip_address", default=None
)
_request_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "audit_request_id", default=None
)
_business_event: contextvars.ContextVar[str] = contextvars.ContextVar(
    "audit_business_event", default=""
)
_suspended: contextvars.ContextVar[bool] = contextvars.ContextVar("audit_suspended", default=False)


class audit_actor:  # noqa: N801 - used as a context manager
    """Set who is acting, for a block of work.

    ``AuditContextMiddleware`` does this per request. Management commands and
    Celery tasks should do it explicitly, or their changes are attributed to
    "system", which is honest but unhelpful.
    """

    def __init__(
        self,
        user_id: int | None = None,
        kind: str = "user",
        impersonated_by_user_id: int | None = None,
        ip_address: str | None = None,
        request_id: uuid.UUID | None = None,
    ):
        self._values = {
            _actor_user_id: user_id,
            _actor_kind: kind,
            _impersonated_by_user_id: impersonated_by_user_id,
            _ip_address: ip_address,
            _request_id: request_id,
        }
        self._tokens: list = []

    def __enter__(self):
        self._tokens = [(var, var.set(value)) for var, value in self._values.items()]
        return self

    def __exit__(self, *exc):
        for var, token in reversed(self._tokens):
            var.reset(token)
        return False


class audit_event:  # noqa: N801 - used as a context manager
    """Label the changes inside this block with a business event.

    Without it an audit row says "tenant_membership.revoked_at changed". With it
    the row says that happened during ``ownership_transfer_accepted``, which is
    the difference between a log you can read and a log you can only grep.
    """

    def __init__(self, name: str):
        self._name = name
        self._token = None

    def __enter__(self):
        self._token = _business_event.set(self._name)
        return self

    def __exit__(self, *exc):
        _business_event.reset(self._token)
        return False


class audit_suspended:  # noqa: N801 - used as a context manager
    """Stop writing audit rows for a block. Bulk reference-data loads only.

    Loading a tax year's PAYE brackets is one human action, recorded once by the
    loader in ``reference_data_version``. Emitting four hundred audit rows for it
    buries the rows that matter. Every use must be justified in review, and it is
    named so it is obvious there.
    """

    def __init__(self):
        self._token = None

    def __enter__(self):
        self._token = _suspended.set(True)
        return self

    def __exit__(self, *exc):
        _suspended.reset(self._token)
        return False


# --------------------------------------------------------------------- masking

# The safety net. Any field whose name contains one of these is masked even when
# the model forgets to declare it. Add to this list rather than relying on every
# future model author to remember.
SENSITIVE_NAME_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "_hash",
    "id_number",
    "account_number",
    "mfa",
    "otp",
    "code",
)

# Changes on every save and says nothing a reader wants. The actor and timestamp
# of the audit row itself already carry this information.
DEFAULT_EXCLUDED_FIELDS = ("updated_at", "updated_by_user")

MASKED = "***"


def is_sensitive(model, field_name: str) -> bool:
    declared = set(getattr(model, "audit_sensitive_fields", ()))
    if field_name in declared:
        return True
    lowered = field_name.lower()
    return any(fragment in lowered for fragment in SENSITIVE_NAME_FRAGMENTS)


def _excluded(model) -> set[str]:
    return set(DEFAULT_EXCLUDED_FIELDS) | set(getattr(model, "audit_exclude_fields", ()))


def to_jsonable(value):
    """Make a field value safe for JSONB without losing precision.

    ``Decimal`` becomes a string, never a float. Invariant 6 says no floating
    point anywhere near money, and that applies to the audit trail too — a
    rounded figure in the history is a figure you cannot reconcile against.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        # Should not occur; if it does, the model has a FloatField near money.
        return repr(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, dict | list):
        return value
    return str(value)


# ---------------------------------------------------------------- the registry


class AuditedModel(models.Model):
    """Marker base. Inheriting it is what turns on change history.

    Two optional class attributes:

    ``audit_sensitive_fields``
        Names whose values must never be written to the audit trail. Additive to
        ``SENSITIVE_NAME_FRAGMENTS``.

    ``audit_exclude_fields``
        Names not worth recording at all. Additive to
        ``DEFAULT_EXCLUDED_FIELDS``. Use sparingly — an excluded field is one you
        cannot answer questions about later.
    """

    audit_sensitive_fields: tuple[str, ...] = ()
    audit_exclude_fields: tuple[str, ...] = ()

    class Meta:
        abstract = True


def audited_models():
    from django.apps import apps

    return [m for m in apps.get_models() if issubclass(m, AuditedModel) and not m._meta.abstract]


# ------------------------------------------------------------------- snapshots

SNAPSHOT_ATTR = "_audit_snapshot"


def _tracked_fields(model):
    """Concrete fields worth recording, by attname so FKs give the raw id."""
    excluded = _excluded(model)
    return [
        f
        for f in model._meta.concrete_fields
        if f.name not in excluded and f.attname not in excluded
    ]


def _snapshot(instance) -> dict:
    """Current in-memory values, skipping deferred fields.

    A deferred field was never loaded, so it has no "before" value and reading it
    would trigger a query — which is exactly what this module exists to avoid.
    """
    values = {}
    for field in _tracked_fields(type(instance)):
        if field.attname in instance.__dict__:
            values[field.attname] = instance.__dict__[field.attname]
    return values


# ------------------------------------------------------------------- receivers


def _write(instance, operation: str, changed_fields: dict, event: str | None = None):
    from core.models import AuditLog

    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is None:
        # The changed row is not tenant-scoped (a Tenant, a platform setting).
        # Attribute the audit row to whatever tenant is pinned, so a tenant's
        # own actions stay in its own history.
        from core.managers import get_current_tenant_id

        tenant_id = get_current_tenant_id()

    AuditLog.all_tenants.create(
        tenant_id=tenant_id,
        actor_user_id=_actor_user_id.get(),
        actor_kind=_actor_kind.get(),
        impersonated_by_user_id=_impersonated_by_user_id.get(),
        table_name=instance._meta.db_table,
        record_pk=instance.pk,
        operation=operation,
        changed_fields=changed_fields,
        business_event=event or _business_event.get(),
        ip_address=_ip_address.get(),
        request_id=_request_id.get(),
    )


def _write_read_event(instance, *, business_event: str, detail: dict | None = None):
    """Record that a row was READ. For documents, not for ordinary queries.

    A subject access request under POPIA asks who looked at a document, not only
    who changed it, and a payslip disclosure is a read. Auditing every SELECT in
    the system would be useless noise; auditing every document download is the
    record that answers the question actually asked.
    """
    _write(instance, "read", detail or {"business_event": business_event}, event=business_event)


def on_post_init(sender, instance, **kwargs):
    if not issubclass(sender, AuditedModel):
        return
    setattr(instance, SNAPSHOT_ATTR, _snapshot(instance))


def on_post_save(sender, instance, created, raw=False, **kwargs):
    if raw or _suspended.get() or not issubclass(sender, AuditedModel):
        return

    model = type(instance)
    after = _snapshot(instance)

    if created:
        changed = {
            name: {"new": MASKED if is_sensitive(model, name) else to_jsonable(value)}
            for name, value in after.items()
        }
        _write(instance, "insert", changed)
        setattr(instance, SNAPSHOT_ATTR, after)
        return

    before = getattr(instance, SNAPSHOT_ATTR, None)
    if before is None:
        # Saved an instance that was constructed rather than loaded, on an
        # existing primary key. There is no trustworthy "before", and inventing
        # one is worse than saying so.
        _write(instance, "update", {"__unknown_previous_state__": True})
        setattr(instance, SNAPSHOT_ATTR, after)
        return

    changed = {}
    for name, new_value in after.items():
        old_value = before.get(name)
        if old_value == new_value:
            continue
        if is_sensitive(model, name):
            changed[name] = {"changed": True, "masked": True}
        else:
            changed[name] = {"old": to_jsonable(old_value), "new": to_jsonable(new_value)}

    if changed:
        _write(instance, "update", changed)
    setattr(instance, SNAPSHOT_ATTR, after)


def on_post_delete(sender, instance, **kwargs):
    if _suspended.get() or not issubclass(sender, AuditedModel):
        return
    model = type(instance)
    changed = {
        name: {"old": MASKED if is_sensitive(model, name) else to_jsonable(value)}
        for name, value in _snapshot(instance).items()
    }
    _write(instance, "delete", changed)


def connect_signals():
    """Wired from ``CoreConfig.ready()``.

    Connected without a ``sender`` so a model added in a later phase is audited
    the day it inherits ``AuditedModel``, with no registration step to forget.
    The receivers return immediately for anything that is not audited.
    """
    from django.db.models.signals import post_delete, post_init, post_save

    post_init.connect(on_post_init, dispatch_uid="core.audit.post_init")
    post_save.connect(on_post_save, dispatch_uid="core.audit.post_save")
    post_delete.connect(on_post_delete, dispatch_uid="core.audit.post_delete")
