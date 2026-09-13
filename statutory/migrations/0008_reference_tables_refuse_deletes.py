from django.db import migrations

from core.db.rls import (
    DROP_NO_DELETE_FUNCTION,
    NO_DELETE_FUNCTION,
    drop_no_delete,
    no_delete,
)

# The reference tables that tenant-scoped tables hold a foreign key into. Every one
# of these is a table an employer, employee or payslip points at.
GUARDED = ["sector", "sector_area", "job_grade", "bank", "bank_branch", "tax_year"]


class Migration(migrations.Migration):
    """Reference tables that tenant tables point at refuse DELETE.

    Found in P3 by a test that expected ``ProtectedError`` and got a successful
    delete. Deleting a sector from a session with no tenant pinned succeeds and
    ORPHANS every employer row that referenced it - Django's PROTECT finds nothing to
    protect because row-level security hides the referencing rows, and PostgreSQL's
    own foreign key check is subject to the policy too because the table has FORCE
    ROW LEVEL SECURITY. Three layers of protection, none of which raises.

    ``platform_context()`` does not help: strict tenant tables carry no platform
    override by decision D-54, so the referencing rows are invisible there as well.

    A BEFORE DELETE trigger on the referenced table is the remaining layer, because it
    runs whatever the deleting session can see. Statutory reference rows are
    superseded rather than deleted anyway (D-56), so this forbids nothing the design
    permitted - it just stops the accident.
    """

    dependencies = [
        ("statutory", "0007_bank_account_length_may_be_unknown"),
        ("employers", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(NO_DELETE_FUNCTION, DROP_NO_DELETE_FUNCTION),
        *[migrations.RunSQL(no_delete(table), drop_no_delete(table)) for table in GUARDED],
    ]
