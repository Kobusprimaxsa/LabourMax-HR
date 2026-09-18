"""D-192 extended to the parental family (P6 chunk 5).

The trigger's message and its list of codes are both built from
``employees/statutory_methods.py``'s own WHY dictionary, so re-running the
CREATE OR REPLACE is the whole change. PARENTAL, MATERNITY and ADOPTION are
prescribed the same way SICK and FAMILY_RESPONSIBILITY are: the reading-in
states a period per birth or placement and offers no accrual method to select.
"""

from django.db import migrations

from employees.statutory_methods import DROP_TRIGGER, TRIGGER_FUNCTION, migration_pre_check


class Migration(migrations.Migration):
    dependencies = [
        ("employees", "0011_accommodation_deduction_percentage"),
        ("leave", "0013_maternity_and_adoption_draw_on_parental"),
    ]

    operations = [
        migrations.RunPython(migration_pre_check, migrations.RunPython.noop),
        migrations.RunSQL(TRIGGER_FUNCTION, DROP_TRIGGER),
    ]
