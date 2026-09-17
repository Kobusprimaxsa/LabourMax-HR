"""D-197: the seeded ACCOM_DED payroll component becomes a percentage of the wage.

Seeding never updates a row, so this carries the change to environments that
already seeded it. Refuses, naming them, if any recurring line already captured
ACCOM_DED as a rand amount. Lives in employees because the pre-check reads
employee_recurring_component.
"""

from django.db import migrations

from employers.catalogue_maintenance import (
    accommodation_deduction_as_fixed,
    accommodation_deduction_as_percentage,
)


def forwards(apps, schema_editor):
    accommodation_deduction_as_percentage(schema_editor.connection)


def backwards(apps, schema_editor):
    accommodation_deduction_as_fixed(schema_editor.connection)


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0010_entitlement_statutory_accrual_method"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
