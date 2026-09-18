"""D-201: MATERNITY and ADOPTION become sub-types of PARENTAL.

``seedleavetypes`` never updates an existing row, so this carries the change to
environments already seeded. System rows are locked by ``lock_system_rows()``,
so the update goes through REFERENCE_MAINTENANCE_VAR inside this migration's own
transaction (documents/0002's precedent, D-92 on the ordering).

Idempotent and conservative: it touches only the two shared SYSTEM rows, only
where they still say ``balance_source='own'``, and only if PARENTAL itself is
present. A tenant's own row coded MATERNITY is its own scheme and is left alone.
"""

from django.db import migrations

from core.db.rls import PLATFORM_VAR, REFERENCE_MAINTENANCE_VAR


def to_sub_types(apps, schema_editor):
    _repoint(schema_editor, parent=True)


def to_own_balance(apps, schema_editor):
    _repoint(schema_editor, parent=False)


def _repoint(schema_editor, *, parent: bool):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [PLATFORM_VAR])
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute(
            "SELECT id FROM leave_type WHERE tenant_id IS NULL AND is_system AND code = 'PARENTAL'"
        )
        row = cursor.fetchone()
        if row is not None:
            parental_id = row[0]
            if parent:
                cursor.execute(
                    """
                    UPDATE leave_type
                       SET parent_leave_type_id = %s, balance_source = 'parent'
                     WHERE tenant_id IS NULL AND is_system
                       AND code IN ('MATERNITY', 'ADOPTION') AND balance_source = 'own'
                    """,
                    [parental_id],
                )
            else:
                cursor.execute(
                    """
                    UPDATE leave_type
                       SET parent_leave_type_id = NULL, balance_source = 'own'
                     WHERE tenant_id IS NULL AND is_system
                       AND code IN ('MATERNITY', 'ADOPTION') AND parent_leave_type_id = %s
                    """,
                    [parental_id],
                )
        cursor.execute("SELECT set_config(%s, 'off', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute("SELECT set_config(%s, 'off', true)", [PLATFORM_VAR])


class Migration(migrations.Migration):
    dependencies = [("leave", "0012_parental_declaration")]

    operations = [migrations.RunPython(to_sub_types, to_own_balance)]
