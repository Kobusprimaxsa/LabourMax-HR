"""Row-level security — layer 2 of three.

PostgreSQL itself refuses to return another tenant's rows. This catches raw SQL,
a bypassed manager, a management command, and any future bug in layer 1.

Emitted by migrations via ``RunSQL(enable_rls(table), disable_rls(table))``.
"""

from __future__ import annotations

POLICY_NAME = "tenant_isolation"
SESSION_VAR = "labourmax.tenant_id"


def enable_rls(table: str) -> str:
    """Enable RLS on a tenant-scoped table and attach the isolation policy.

    FORCE is important: without it the table owner — which is what the
    application usually connects as — silently bypasses the policy.
    """
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
CREATE POLICY {POLICY_NAME} ON {table}
    USING (
        current_setting('{SESSION_VAR}', true) IS NOT NULL
        AND current_setting('{SESSION_VAR}', true) <> ''
        AND tenant_id = current_setting('{SESSION_VAR}', true)::bigint
    )
    WITH CHECK (
        current_setting('{SESSION_VAR}', true) IS NOT NULL
        AND current_setting('{SESSION_VAR}', true) <> ''
        AND tenant_id = current_setting('{SESSION_VAR}', true)::bigint
    );
"""


def disable_rls(table: str) -> str:
    return f"""
DROP POLICY IF EXISTS {POLICY_NAME} ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


def append_only(table: str) -> str:
    """Revoke UPDATE and DELETE. For audit_log, leave_transaction and friends.

    Invariant 4: finalised financial records and ledgers are never rewritten.
    Corrections insert reversals.
    """
    return f"REVOKE UPDATE, DELETE ON {table} FROM PUBLIC;"
