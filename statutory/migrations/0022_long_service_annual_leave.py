"""BCCCI clause 9.1(b): 28 days' annual leave for more than ten years' service.

The first long-service annual leave band in this schema (D-243). Paired with a
NOT NULL boolean carrying no default, D-198's own shape: every rule set loaded
before this migration was read against an instrument that states no such band,
so FALSE is the truth for all of them — and a row written after it has to say.
"""

from django.db import migrations, models


def to_pair(apps, schema_editor):
    """Every existing rule set comes from an instrument with no long-service band.

    The BCEA, SD1 and SD7 all give one annual leave entitlement regardless of
    service length. This is a statement about what those instruments say, not a
    convenient default — which is why the boolean loses its default immediately
    afterwards and a new row must state its own answer.
    """
    rule_set = apps.get_model("statutory", "LeaveRuleSet")
    rule_set.objects.filter(has_long_service_annual_leave__isnull=True).update(
        has_long_service_annual_leave=False
    )


def to_sentinel(apps, schema_editor):
    rule_set = apps.get_model("statutory", "LeaveRuleSet")
    rule_set.objects.update(has_long_service_annual_leave=None)


class Migration(migrations.Migration):
    dependencies = [("statutory", "0021_notice_unit_blank_when_contested")]

    operations = [
        migrations.AddField(
            model_name="leaveruleset",
            name="has_long_service_annual_leave",
            field=models.BooleanField(null=True),
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="long_service_annual_leave_years",
            field=models.SmallIntegerField(
                blank=True,
                null=True,
                help_text="Years of service the longer entitlement is stated against.",
            ),
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="long_service_years_inclusive",
            field=models.BooleanField(
                blank=True,
                null=True,
                help_text="Does the stated year count itself fall in the LONGER band?",
            ),
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="long_service_annual_leave_days_5day",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="long_service_annual_leave_days_6day",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True),
        ),
        migrations.RunPython(to_pair, to_sentinel),
        migrations.AlterField(
            model_name="leaveruleset",
            name="has_long_service_annual_leave",
            field=models.BooleanField(
                help_text=(
                    "Does this instrument give a longer annual leave entitlement for long "
                    "service? FALSE means the instrument states none (the BCEA, SD1 and "
                    "SD7), not that long service earns nothing. No default: a row that "
                    "does not say fails."
                )
            ),
        ),
        migrations.AddConstraint(
            model_name="leaveruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(has_long_service_annual_leave=False)
                | (
                    models.Q(long_service_annual_leave_years__isnull=False)
                    & models.Q(long_service_years_inclusive__isnull=False)
                    & models.Q(long_service_annual_leave_days_5day__isnull=False)
                    & models.Q(long_service_annual_leave_days_6day__isnull=False)
                ),
                name="leave_rule_set_long_service_band_states_its_figures",
            ),
        ),
        migrations.AddConstraint(
            model_name="leaveruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(has_long_service_annual_leave=True)
                | (
                    models.Q(long_service_annual_leave_years__isnull=True)
                    & models.Q(long_service_years_inclusive__isnull=True)
                    & models.Q(long_service_annual_leave_days_5day__isnull=True)
                    & models.Q(long_service_annual_leave_days_6day__isnull=True)
                ),
                name="leave_rule_set_no_long_service_band_states_no_figures",
            ),
        ),
    ]
