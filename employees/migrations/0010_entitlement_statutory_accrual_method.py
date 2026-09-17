"""D-192: refuse an accrual method the statute does not offer, for the system
SICK and FAMILY_RESPONSIBILITY leave types.

The pre-check runs FIRST and refuses, naming every offending row, if any stored
row already violates the rule. It never rewrites one. A trigger rather than a
CHECK because the leave type's code and is_system are on another table.
"""

from django.db import migrations

from employees.statutory_methods import DROP_TRIGGER, TRIGGER_FUNCTION, migration_pre_check


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0009_restored_note_and_percentage_guards"),
        ("leave", "0011_leave_application_unpaid_hours"),
    ]

    operations = [
        migrations.RunPython(migration_pre_check, migrations.RunPython.noop),
        migrations.RunSQL(TRIGGER_FUNCTION, DROP_TRIGGER),
    ]
