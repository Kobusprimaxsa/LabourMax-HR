"""Changes to the shared payroll component catalogue that seeding cannot make.

``seedcomponents`` never updates an existing row (a finalised payslip line points
at these), so a decision that changes a seeded row travels as a migration calling
a function here — the documents/0002 precedent. System rows are locked by
``lock_system_rows()``; the change goes through ``REFERENCE_MAINTENANCE_VAR``, set
and cleared inside the migration's own transaction (D-92).

A change that would contradict data already captured REFUSES, naming the rows,
and changes nothing. It never rewrites captured data.
"""

from __future__ import annotations

from core.db.rls import PLATFORM_VAR, REFERENCE_MAINTENANCE_VAR
from core.managers import TENANT_SESSION_VAR


class AccommodationDeductionMigrationError(RuntimeError):
    """Recurring lines already captured ACCOM_DED as a rand amount."""


def _captured_rand_amounts(cursor) -> list[tuple]:
    """Every ACCOM_DED recurring line carrying an amount, across EVERY tenant.
    employee_recurring_component is strictly tenant-scoped under FORCE RLS, so a
    migration session with no tenant pinned sees none of it — walk the tenants."""
    found = []
    cursor.execute("SELECT id FROM tenant ORDER BY id")
    for (tenant_id,) in cursor.fetchall():
        cursor.execute("SELECT set_config(%s, %s, true)", [TENANT_SESSION_VAR, str(tenant_id)])
        cursor.execute(
            """
            SELECT r.id, r.employee_id, r.amount
              FROM employee_recurring_component r
              JOIN payroll_component c ON c.id = r.payroll_component_id
             WHERE c.is_system AND c.code = 'ACCOM_DED' AND r.amount IS NOT NULL
             ORDER BY r.id
            """
        )
        found += [(tenant_id, *row) for row in cursor.fetchall()]
    cursor.execute("SELECT set_config(%s, '', true)", [TENANT_SESSION_VAR])
    return found


def accommodation_deduction_as_percentage(connection) -> None:
    """D-197: ACCOM_DED becomes percentage_of_base. Refuses if any line already
    captured it as a rand amount."""
    with connection.cursor() as cursor:
        captured = _captured_rand_amounts(cursor)
        if captured:
            lines = "\n".join(
                f"  - tenant {t}, employee {e}, recurring line {r}: amount R{a}"
                for t, r, e, a in captured
            )
            raise AccommodationDeductionMigrationError(
                f"{len(captured)} recurring line(s) captured the accommodation deduction as a "
                f"rand amount. ACCOM_DED is now a percentage of the wage (D-197); this "
                f"migration refuses rather than converting captured figures. Close each line "
                f"and capture its percentage, then migrate again:\n{lines}"
            )
        cursor.execute("SELECT set_config(%s, 'on', true)", [PLATFORM_VAR])
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute(
            "UPDATE payroll_component SET calculation_method = 'percentage_of_base' "
            "WHERE tenant_id IS NULL AND is_system AND code = 'ACCOM_DED'"
        )
        cursor.execute("SELECT set_config(%s, 'off', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute("SELECT set_config(%s, 'off', true)", [PLATFORM_VAR])


def accommodation_deduction_as_fixed(connection) -> None:
    """The reverse, for unapplying the migration."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [PLATFORM_VAR])
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute(
            "UPDATE payroll_component SET calculation_method = 'fixed' "
            "WHERE tenant_id IS NULL AND is_system AND code = 'ACCOM_DED'"
        )
        cursor.execute("SELECT set_config(%s, 'off', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute("SELECT set_config(%s, 'off', true)", [PLATFORM_VAR])
