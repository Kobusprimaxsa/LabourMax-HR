"""Row-level security — layer 2 of three.

PostgreSQL itself refuses to return another tenant's rows. This catches raw SQL,
a bypassed manager, a management command, and any future bug in layer 1.

Emitted by migrations via ``RunSQL(enable_rls(table), disable_rls(table))``.

Two policies, matching the two model bases in ``core/managers.py``:

``enable_rls``
    For ``TenantScopedModel`` tables. ``tenant_id`` is NOT NULL and a row is
    visible only to its own tenant. No exceptions, no platform override — the
    platform console reads employer data through a tenant context, not around it.

``enable_rls_optional``
    For ``TenantOptionalModel`` tables, where ``tenant_id`` is nullable because
    the row may predate the tenant being known. Adds exactly two carve-outs, and
    both are visible in the SQL rather than implied by application code.

If you change a policy here, change the matching manager in ``core/managers.py``
in the same commit.
"""

from __future__ import annotations

POLICY_NAME = "tenant_isolation"
SESSION_VAR = "labourmax.tenant_id"
PLATFORM_VAR = "labourmax.platform_access"

# Reads as: the session variable is set to a usable value.
_TENANT_PINNED = f"""coalesce(current_setting('{SESSION_VAR}', true), '') <> ''"""

# nullif() rather than a bare cast, and this is not cosmetic. When no tenant is
# pinned the session variable is the empty string, and ''::bigint RAISES
# "invalid input syntax for type bigint". SQL does not guarantee left-to-right
# evaluation, so the planner is free to evaluate the cast before the guard that
# was supposed to protect it — which it does. nullif() yields NULL instead, the
# comparison yields NULL, and the policy treats that as "no rows".
#
# Failing closed matters more than failing loudly here: an unscoped query should
# return nothing, not blow up in the middle of an unrelated request.
_TENANT_MATCHES = f"""tenant_id = nullif(current_setting('{SESSION_VAR}', true), '')::bigint"""
_PLATFORM = f"""coalesce(current_setting('{PLATFORM_VAR}', true), 'off') = 'on'"""


def enable_rls(table: str) -> str:
    """Strict isolation for a NOT NULL ``tenant_id``.

    FORCE is important: without it the table owner — which is what the
    application connects as — silently bypasses the policy, so the structural
    test would pass while the protection did nothing at all.
    """
    predicate = f"({_TENANT_PINNED} AND {_TENANT_MATCHES})"
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
CREATE POLICY {POLICY_NAME} ON {table}
    USING {predicate}
    WITH CHECK {predicate};
"""


def enable_rls_optional(table: str) -> str:
    """Isolation for a nullable ``tenant_id``.

    - platform context (inside ``platform_context()``) sees every row
    - a tenant session sees only its own rows, **never** the NULL-tenant ones
    - a session with no tenant pinned sees the NULL-tenant rows only

    That third rule is uniform across these tables, and the uniformity is a
    correction rather than a simplification. The first attempt made NULL-tenant
    rows writable-but-not-readable on ``audit_log`` and ``background_job``, on the
    reasoning that registration writes them and nothing should read them back.
    It does not work: Django appends ``RETURNING id`` to every INSERT, and
    PostgreSQL applies the policy's USING clause to rows returned that way. A
    row the USING clause rejects therefore cannot be inserted at all — reported,
    confusingly, as "new row violates row-level security policy".

    What is given up is a secondary defence: an unauthenticated request that
    somehow reached ``AuditLog.objects`` could read platform-level audit rows.
    What is kept is the primary one: no tenant can ever see another tenant's
    rows, or the platform's. The trade was made knowingly, because the
    alternative was raw SQL inserts for audit rows — fragile in a different and
    less visible way.

    ``TenantOptionalManager`` mirrors these three rules exactly. Change one,
    change the other.
    """
    predicate = (
        f"({_PLATFORM}"
        f"\n        OR ({_TENANT_PINNED} AND {_TENANT_MATCHES})"
        f"\n        OR (tenant_id IS NULL AND NOT {_TENANT_PINNED}))"
    )
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
CREATE POLICY {POLICY_NAME} ON {table}
    USING {predicate}
    WITH CHECK {predicate};
"""


def disable_rls(table: str) -> str:
    """Reverse operation for both policy variants."""
    return f"""
DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


MAINTENANCE_VAR = "labourmax.allow_ledger_maintenance"

# noqa justification: this is DDL assembled from module constants and migration
# arguments, never from user input. There is no query parameter to bind.
APPEND_ONLY_FUNCTION = f"""
CREATE OR REPLACE FUNCTION labourmax_append_only() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('{MAINTENANCE_VAR}', true), 'off') = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION 'This table is append-only: UPDATE and DELETE are not permitted. '
        'Corrections insert a reversing row (CLAUDE.md invariant 4). Statutory '
        'retention purges set {MAINTENANCE_VAR} deliberately.';
END;
$$ LANGUAGE plpgsql;
"""  # noqa: S608

DROP_APPEND_ONLY_FUNCTION = "DROP FUNCTION IF EXISTS labourmax_append_only();"


def append_only(table: str) -> str:
    """Make a table append-only. For audit_log, leave_transaction and friends.

    Invariant 4: finalised financial records and ledgers are never rewritten.
    Corrections insert reversals.

    This used to be ``REVOKE UPDATE, DELETE ON {table} FROM PUBLIC``, which does
    **nothing**: a table's owner holds its privileges implicitly and is not
    affected by a revoke against PUBLIC, and the application connects as the
    owner. Exactly the same trap as row-level security without FORCE — the
    protection reads convincingly and is absent. A trigger binds the owner too.

    The one deliberate exception is the POPIA retention purge (P11), which must
    be able to delete. It sets ``labourmax.allow_ledger_maintenance`` for the
    duration of the purge, in the same style as ``platform_context()``: one
    named, greppable, logged hole rather than a permanently writable ledger.

    Requires ``APPEND_ONLY_FUNCTION`` to have been run once in an earlier
    operation of the same migration.
    """
    return f"""
DROP TRIGGER IF EXISTS {table}_append_only ON {table};
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION labourmax_append_only();
"""


def drop_append_only(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"


# ------------------------------------------------------- reference delete guard

REFERENCE_MAINTENANCE_VAR = "labourmax.allow_reference_maintenance"

NO_DELETE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION labourmax_no_delete() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('{REFERENCE_MAINTENANCE_VAR}', true), 'off') = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'This reference table cannot be deleted from. Tenant tables '
        'reference it, and FORCE ROW LEVEL SECURITY makes the foreign key check '
        'subject to the policy - so a session that cannot see the referencing rows '
        'deletes the row and orphans them, with neither PROTECT nor the foreign key '
        'raising. Supersede the row with a new effective-dated one instead. Deliberate '
        'maintenance sets {REFERENCE_MAINTENANCE_VAR}.';
END;
$$ LANGUAGE plpgsql;
"""  # noqa: S608

DROP_NO_DELETE_FUNCTION = "DROP FUNCTION IF EXISTS labourmax_no_delete();"


def no_delete(table: str) -> str:
    """Refuse DELETE on a reference table that tenant-scoped tables point at.

    **The trap this closes, found in P3 and reproduced before it was fixed.**
    ``employer.sector_id`` is ``on_delete=PROTECT`` and PostgreSQL carries its own
    foreign key constraint. Neither stops ``Sector.objects.get(...).delete()`` from a
    session with no tenant pinned:

    - Django's collector queries ``employer WHERE sector_id = X`` to find the rows it
      must protect. Row-level security returns **none**, so it finds nothing to
      protect and proceeds.
    - PostgreSQL's referential integrity check is itself subject to the policy when
      the table has ``FORCE ROW LEVEL SECURITY``, so it also sees no referencing row.

    The sector is deleted, the employer row keeps a ``sector_id`` pointing at nothing,
    and not one of the three layers raises. ``platform_context()`` does not help
    either: by decision D-54 the strict tenant tables carry no platform override, so
    the referencing rows stay invisible there too.

    A trigger on the referenced table is the only layer left, because it runs
    regardless of what the deleting session can see.

    Requires ``NO_DELETE_FUNCTION`` to have been run once in an earlier operation of
    the same migration.
    """
    return f"""
DROP TRIGGER IF EXISTS {table}_no_delete ON {table};
CREATE TRIGGER {table}_no_delete
    BEFORE DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION labourmax_no_delete();
"""


def drop_no_delete(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table};"
