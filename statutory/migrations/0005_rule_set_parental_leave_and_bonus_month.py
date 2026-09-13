from django.db import migrations, models


class Migration(migrations.Migration):
    """Three corrections to the rule sets, all found by reading the source documents.

    **Parental leave is months plus days, not a day count.** Van Wyk v Minister of
    Employment and Labour [2025] ZACC 20 gives all parents together four months and
    ten days. Four calendar months from 15 January is not the same number of days as
    four from 15 June, so a single `parental_leave_total_days` column would have
    forced one interpretation of "four months" into the reference data, where it
    would then have been impossible to tell from a figure Parliament had legislated.

    **The maternity rule was stored backwards.** The column was
    `maternity_pre_birth_reserved_days`. The statute's hard rule is the opposite way
    round: a birth mother may not work for six weeks *after* the birth unless
    certified fit, and separately may *start* leave up to four weeks *before* the
    expected date. Two rules, two columns, and the post-birth one is the one with
    teeth.

    **A sector with no statutory bonus now stores NULL rather than a meaningless
    month.** `annual_bonus_month` was NOT NULL with a 1-12 CHECK, so the BCEA default
    and the domestic sector - neither of which has a statutory annual bonus - would
    have had to name a month they never pay in.

    Written by hand rather than generated: the autodetector asks for a default on
    each new NOT NULL column, and a default on a rule set column is a statutory
    figure living in a migration, which CLAUDE.md forbids. The tables are empty in
    every environment, so `preserve_default=False` adds the columns with a throwaway
    zero that never reaches the model.

    These are departures from the workbook's column dictionary. The workbook needs
    the same three corrections - see D-64.
    """

    dependencies = [
        ("statutory", "0004_alter_sectorarea_code"),
    ]

    operations = [
        migrations.RemoveField(model_name="leaveruleset", name="parental_leave_total_days"),
        migrations.RemoveField(model_name="leaveruleset", name="maternity_pre_birth_reserved_days"),
        migrations.AddField(
            model_name="leaveruleset",
            name="parental_leave_total_months",
            field=models.SmallIntegerField(
                default=0, help_text="Calendar months. Four, under the interim reading-in."
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="parental_leave_additional_days",
            field=models.SmallIntegerField(
                default=0, help_text="Days on top of the months. Ten, under the interim reading-in."
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="maternity_earliest_start_weeks_before_birth",
            field=models.SmallIntegerField(
                default=0,
                help_text="How early leave may begin before the expected date of birth.",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="leaveruleset",
            name="maternity_no_work_weeks_after_birth",
            field=models.SmallIntegerField(
                default=0,
                help_text=(
                    "A birth mother may not work for this many weeks after the birth unless "
                    "a medical practitioner certifies her fit. Not merely leave she may take."
                ),
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="terminationruleset",
            name="annual_bonus_month",
            field=models.SmallIntegerField(
                blank=True,
                null=True,
                help_text=(
                    "Calendar month of normal payment. NULL where the sector has no "
                    "statutory bonus at all - which is the domestic sector and the BCEA "
                    "default."
                ),
            ),
        ),
        migrations.RemoveConstraint(
            model_name="terminationruleset",
            name="termination_annual_bonus_month_is_a_month",
        ),
        migrations.AddConstraint(
            model_name="terminationruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(("annual_bonus_month__isnull", True))
                | models.Q(("annual_bonus_month__gte", 1), ("annual_bonus_month__lte", 12)),
                name="termination_annual_bonus_month_is_a_month",
            ),
        ),
    ]
