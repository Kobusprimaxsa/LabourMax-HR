"""D-132: an engagement may be current while carrying a future termination date.

The CHECK dropped here said ``is_current`` implies no termination date. That is
exactly backwards for the case the flag exists to express — an employee serving a
month's notice, whose termination is captured today and takes effect in October.
Under the old constraint the only way to record the termination was to close the
engagement immediately, which removed the person from every "current employee"
query including the payroll run that still owes them a final salary.

It is not replaced with a date-aware CHECK. ``termination_date >= CURRENT_DATE``
would get **stricter** as time passes, so a row that was valid the day it was
written would fail the next table rewrite, restore or ``VALIDATE CONSTRAINT`` —
the opposite direction to ``employee_born_before_today``, which only ever becomes
more permissive and is safe for that reason.

What has to hold is one current engagement per employee, and that is the partial
unique ``uniq_current_engagement_per_employee``, untouched.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0005_employeeleaveentitlement_employeenote_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="employeeengagement",
            name="engagement_current_means_not_yet_terminated",
        ),
    ]
