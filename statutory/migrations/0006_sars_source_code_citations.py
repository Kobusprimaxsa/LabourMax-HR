from django.db import migrations, models


class Migration(migrations.Migration):
    """``sars_source_code`` becomes a cited table.

    The code and its wording are SARS's. The four base flags are not: whether an
    amount enters the UIF, SDL or COIDA base is a reading of three statutes that each
    define "remuneration" or "earnings" differently, and commission is the standing
    proof - taxable, in the SDL base, and **out** of the UIF base.

    A boolean with no citation beside it is a compliance decision nobody can audit
    and nobody dares change. So the table gains ``source_reference`` (with the same
    not-blank CHECK as every other cited table), ``source_url`` and ``notes``, and
    the reasoning for each flag is recorded on the row that carries it.

    The table is empty in every environment, so the columns are added with
    ``preserve_default=False`` and nothing needs backfilling.
    """

    dependencies = [
        ("statutory", "0005_rule_set_parental_leave_and_bonus_month"),
    ]

    operations = [
        migrations.AddField(
            model_name="sarssourcecode",
            name="source_reference",
            field=models.CharField(
                default="",
                max_length=200,
                help_text=(
                    "Where this figure comes from, precisely enough to find it again. "
                    "e.g. 'GN 7083, GG 54075, 2 Feb 2026' or 'BCCCI Collective Agreement "
                    "2026, cl 8'."
                ),
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="sarssourcecode",
            name="source_url",
            field=models.URLField(
                blank=True,
                max_length=400,
                help_text=(
                    "Optional. Not every gazette is online, and a fabricated link is "
                    "worse than none."
                ),
            ),
        ),
        migrations.AddField(
            model_name="sarssourcecode",
            name="notes",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Interpretation notes \u2014 how the figure was read, and anything "
                    "ambiguous about it."
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="sarssourcecode",
            constraint=models.CheckConstraint(
                condition=models.Q(("source_reference", ""), _negated=True),
                name="sars_source_code_source_reference_not_blank",
            ),
        ),
    ]
