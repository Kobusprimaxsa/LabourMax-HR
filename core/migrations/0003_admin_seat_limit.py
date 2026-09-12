"""Enforce the administrative user limit in the database.

The rule from the brief is "the first registering user gets admin rights and may
add exactly one more". ``tenant.max_admin_users`` already defaults to 2 with a
CHECK on its range, but a default is not enforcement: nothing stopped a third
membership row being written.

Three layers, as with tenant isolation:

1. ``tenant.max_admin_users`` — the limit as data, so an account can be varied
   without a code change
2. this trigger — the authority, and the only layer that is safe under
   concurrency, because it locks the tenant row before counting
3. ``core.seats.assert_seat_available`` — so a user meets a sentence rather than
   a 500

The trigger is what makes the rule true. The other two make it usable.
"""

from django.db import migrations

from core.db.constraints import (
    ADMIN_SEAT_LIMIT_FUNCTION,
    ADMIN_SEAT_LIMIT_TRIGGER,
    DROP_ADMIN_SEAT_LIMIT_FUNCTION,
    DROP_ADMIN_SEAT_LIMIT_TRIGGER,
)


class Migration(migrations.Migration):
    dependencies = [("core", "0002_harden_rls_casts")]

    operations = [
        migrations.RunSQL(ADMIN_SEAT_LIMIT_FUNCTION, DROP_ADMIN_SEAT_LIMIT_FUNCTION),
        migrations.RunSQL(ADMIN_SEAT_LIMIT_TRIGGER, DROP_ADMIN_SEAT_LIMIT_TRIGGER),
    ]
