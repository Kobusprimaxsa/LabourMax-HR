"""pay_group.public_uid (D-296) - an addition to sheet 02, for the grid's URL.

Added as DDL with a VOLATILE database default so every existing row gets its
own value. A Django AddField computes one default for all rows (duplicates
under UNIQUE), and a data migration cannot backfill: pay_group is under FORCE
row-level security, so an UPDATE with no tenant pinned sees no rows at all.
DDL is not subject to RLS. The database default is dropped afterwards; the
model's uuid4 default supplies new rows, as for employer and employee.
"""

from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("employers", "0004_payroll_component"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE pay_group ADD COLUMN public_uid uuid NOT NULL "
                    "DEFAULT gen_random_uuid(); "
                    "ALTER TABLE pay_group ADD CONSTRAINT pay_group_public_uid_key "
                    "UNIQUE (public_uid); "
                    "ALTER TABLE pay_group ALTER COLUMN public_uid DROP DEFAULT;",
                    reverse_sql="ALTER TABLE pay_group DROP COLUMN public_uid;",
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="paygroup",
                    name="public_uid",
                    field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
            ],
        ),
    ]
