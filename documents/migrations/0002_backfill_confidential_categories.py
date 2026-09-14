from django.db import migrations

from core.db.rls import PLATFORM_VAR, REFERENCE_MAINTENANCE_VAR

# D-143. Set once, here, rather than only in documents/categories.py, because
# seed_system_categories() never updates an existing row — a tenant that had
# already run `seeddocumentcategories` before this decision would otherwise
# never pick it up. `python manage.py migrate` is a command every environment
# runs anyway, so the fix travels with the code rather than needing a second,
# easy-to-forget manual step.
CONFIDENTIAL_BY_DEFAULT_CODES = [
    "ID_COPY",
    "PASSPORT",
    "WORK_PERMIT",
    "ASYLUM_PERMIT",
    "POLICE_CLEARANCE",
    "BANK_CONFIRMATION_EMPLOYEE",
    "NEXT_OF_KIN_FORM",
]


def backfill(apps, schema_editor):
    DocumentCategory = apps.get_model("documents", "DocumentCategory")

    # Two separate holes, both needed. REFERENCE_MAINTENANCE_VAR opens the
    # system-row lock TRIGGER (lock_system_rows) — the same escape hatch
    # employers/components.py's _activate_severance uses. PLATFORM_VAR is a
    # second, independent requirement: these are SHARED rows (tenant_id IS
    # NULL), and enable_rls_shared()'s WITH CHECK clause refuses a write to one
    # from any session that is not inside platform_context() — a migration has
    # no tenant pinned and no platform flag set, so without this the UPDATE is
    # rejected by the POLICY before the trigger is ever reached. `is_local=true`
    # because set_config is transaction-local and Django wraps this migration
    # in one already.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [PLATFORM_VAR])
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        DocumentCategory.objects.filter(
            tenant__isnull=True,
            code__in=CONFIDENTIAL_BY_DEFAULT_CODES,
            is_confidential_by_default=False,
        ).update(is_confidential_by_default=True)
        cursor.execute("SELECT set_config(%s, 'off', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute("SELECT set_config(%s, 'off', true)", [PLATFORM_VAR])


def unbackfill(apps, schema_editor):
    DocumentCategory = apps.get_model("documents", "DocumentCategory")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, 'on', true)", [PLATFORM_VAR])
        cursor.execute("SELECT set_config(%s, 'on', true)", [REFERENCE_MAINTENANCE_VAR])
        DocumentCategory.objects.filter(
            tenant__isnull=True, code__in=CONFIDENTIAL_BY_DEFAULT_CODES
        ).update(is_confidential_by_default=False)
        cursor.execute("SELECT set_config(%s, 'off', true)", [REFERENCE_MAINTENANCE_VAR])
        cursor.execute("SELECT set_config(%s, 'off', true)", [PLATFORM_VAR])


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
