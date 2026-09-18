"""Accrual methods the statute does not offer, refused at capture (D-192).

BCEA s20(2) is the only place the Act lets an agreement select how leave
accrues, and it is annual leave: (b) one day per seventeen days worked, (c) one
hour per seventeen hours worked. Sick leave (s22(2)) and family responsibility
leave (s27(2)) have no method to select, so an entitlement row naming anything
but the default records an agreement that cannot exist.

Three layers, one source for the wording:

- ``refusal_message()`` — what ``EmployeeLeaveEntitlement.clean()`` raises
- ``TRIGGER_FUNCTION`` — a BEFORE INSERT OR UPDATE trigger. A CHECK cannot do
  this: the method is on the entitlement row, but the leave type's code and
  ``is_system`` are only reachable through ``leave_type_id``, and a CHECK cannot
  read another table. The trigger holds whichever path wrote the row — the bulk
  importer, a data migration, psql — and its message is built from the same
  Python text below
- ``refuse_existing_violations()`` — the migration's pre-check. A trigger does
  not look at rows already stored, and those must be refused by name, never
  rewritten: the loader never updates a row, and a migration silently
  normalising captured data is the same violation wearing a different hat

Keyed on SYSTEM rows only (``is_system``): ``leave_type`` is shared (D-87) and a
tenant may define its own row with any code (D-127). The engine separately
ignores the method for these codes (D-190), so a row that somehow predates the
trigger still cannot produce a wrong figure — belt and braces.
"""

from __future__ import annotations

from core.managers import TENANT_SESSION_VAR

TRIGGER_NAME = "employee_leave_entitlement_statutory_method"
DEFAULT_METHOD = "monthly"

# The one place the reason is written. The trigger's messages are built from it.
WHY = {
    "SICK": (
        "Sick leave is BCEA s22(2): the days normally worked in six weeks. s22(3)'s one day "
        "per 26 days worked restricts availability in the first six months; it is not a method."
    ),
    "FAMILY_RESPONSIBILITY": (
        "Family responsibility leave is BCEA s27(2): a flat number of days per annual leave cycle."
    ),
    # Added with P6 chunk 5: the parental family is prescribed in the same way.
    # Van Wyk's reading-in states a period per birth or placement - four months,
    # or four months and ten days in the aggregate - and offers no method for an
    # agreement to select. It does not accrue at all.
    "PARENTAL": (
        "Parental leave is read-in BCEA s25(1) and s25(4A): a period per birth or placement, "
        "not a bank that accrues."
    ),
    "MATERNITY": (
        "Maternity leave draws on the parental entitlement (read-in BCEA s25), which is a "
        "period per birth, not a bank that accrues."
    ),
    "ADOPTION": (
        "Adoption leave is read-in BCEA s25B(1), which gives the parental leave referred to in "
        "s25(1): a period per placement, not a bank that accrues."
    ),
}


def refusal_message(code: str, method: str) -> str:
    return (
        f"{code} leave cannot use the {method} accrual method. The methods an agreement may "
        f"select are BCEA s20(2)'s, and they are annual leave methods: one day per 17 days "
        f"worked, one hour per 17 hours worked. {WHY[code]} There is no method to select."
    )


def refuses(leave_type, method: str) -> bool:
    return bool(leave_type.is_system) and leave_type.code in WHY and method != DEFAULT_METHOD


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _sql_case() -> str:
    """One WHEN per code, each message built by refusal_message() with the method
    spliced in at run time — so the database says exactly what clean() says."""
    marker = "\x00METHOD\x00"
    branches = []
    for code in WHY:
        before, after = refusal_message(code, marker).split(marker)
        branches.append(
            f"        WHEN {_sql_literal(code)} THEN "
            f"{_sql_literal(before)} || NEW.accrual_method || {_sql_literal(after)}"
        )
    return "\n".join(branches)


# noqa justification: DDL assembled from module constants (WHY, TRIGGER_NAME),
# quoted by _sql_literal(); no caller-supplied value ever reaches it.
TRIGGER_FUNCTION = f"""
CREATE OR REPLACE FUNCTION labourmax_statutory_accrual_method() RETURNS trigger AS $$
DECLARE
    type_code text;
    type_is_system boolean;
BEGIN
    IF NEW.accrual_method = {_sql_literal(DEFAULT_METHOD)} THEN
        RETURN NEW;
    END IF;
    SELECT code, is_system INTO type_code, type_is_system
        FROM leave_type WHERE id = NEW.leave_type_id;
    IF type_is_system AND type_code IN ({", ".join(_sql_literal(c) for c in WHY)}) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = CASE type_code
{_sql_case()}
            END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON employee_leave_entitlement;
CREATE TRIGGER {TRIGGER_NAME}
    BEFORE INSERT OR UPDATE OF accrual_method, leave_type_id ON employee_leave_entitlement
    FOR EACH ROW EXECUTE FUNCTION labourmax_statutory_accrual_method();
"""  # noqa: S608 - DDL assembled from this module's constants, never from input

DROP_TRIGGER = f"""
DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON employee_leave_entitlement;
DROP FUNCTION IF EXISTS labourmax_statutory_accrual_method();
"""


def statutory_method_violations(connection) -> list[dict]:
    """Every stored row the trigger would refuse, across EVERY tenant.

    Walks the tenants one by one, pinning each, because employee_leave_entitlement
    is strictly tenant-scoped under FORCE RLS: a session with no tenant pinned —
    which is every migration — sees none of its rows, and a plain count would
    report zero violations whatever is stored. The shared system leave types are
    visible from any tenant's session. Must run inside a transaction:
    set_config(..., true) is transaction-local (D-92).
    """
    found: list[dict] = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM tenant ORDER BY id")
        tenant_ids = [row[0] for row in cursor.fetchall()]
        for tenant_id in tenant_ids:
            cursor.execute("SELECT set_config(%s, %s, true)", [TENANT_SESSION_VAR, str(tenant_id)])
            cursor.execute(
                """
                SELECT e.id, e.employee_id, t.code, e.accrual_method
                  FROM employee_leave_entitlement e
                  JOIN leave_type t ON t.id = e.leave_type_id
                 WHERE t.is_system AND t.code = ANY(%s) AND e.accrual_method <> %s
                 ORDER BY e.id
                """,
                [list(WHY), DEFAULT_METHOD],
            )
            for entitlement_id, employee_id, code, method in cursor.fetchall():
                found.append(
                    {
                        "tenant_id": tenant_id,
                        "entitlement_id": entitlement_id,
                        "employee_id": employee_id,
                        "leave_type_code": code,
                        "accrual_method": method,
                    }
                )
        cursor.execute("SELECT set_config(%s, '', true)", [TENANT_SESSION_VAR])
    return found


def refuse_existing_violations(connection) -> None:
    """Raise, naming every offending row, if any exist. Rewrites nothing."""
    found = statutory_method_violations(connection)
    if not found:
        return
    lines = "\n".join(
        f"  - tenant {v['tenant_id']}, employee {v['employee_id']}, entitlement "
        f"{v['entitlement_id']}: {v['leave_type_code']} / {v['accrual_method']}"
        for v in found
    )
    raise RuntimeError(
        f"{len(found)} employee_leave_entitlement row(s) name an accrual method the statute "
        f"does not offer (D-192). This migration refuses rather than rewriting captured data; "
        f"correct each row by closing it and capturing a new one, then migrate again:\n{lines}"
    )


def migration_pre_check(apps, schema_editor) -> None:
    refuse_existing_violations(schema_editor.connection)
