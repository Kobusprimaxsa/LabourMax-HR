"""Add ``read`` to audit_log.operation.

A document download is a disclosure, not a change. POPIA subject access requests
ask who LOOKED at a payslip, so ``core.files.open_for_download`` records a read
event. Ordinary SELECTs are not audited — that would be noise that buries the
rows worth reading.

Choices-only change: no column is altered, but Django tracks choices in migration
state, so without this ``makemigrations --check`` in CI fails.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_admin_seat_limit")]

    operations = [
        migrations.AlterField(
            model_name="auditlog",
            name="operation",
            field=models.CharField(
                choices=[
                    ("insert", "Insert"),
                    ("update", "Update"),
                    ("delete", "Delete"),
                    ("read", "Read"),
                ],
                max_length=10,
            ),
        ),
    ]
