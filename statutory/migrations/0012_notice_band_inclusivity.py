from django.db import migrations, models


class Migration(migrations.Migration):
    """D-158 corrected: inclusivity is per boundary, read from the statute.

    ``termination_notice_band`` gains ``service_from_inclusive`` (required —
    every band states its own lower-boundary inclusivity) and
    ``service_to_inclusive`` (NULL only for the open-ended top band). Both
    are added with no default, matching this table's own "no rule set
    column carries a default" discipline; that is safe here because the
    table is empty at the point this migration runs — the seven rows loaded
    under D-68 carried no opinion on inclusivity and are cleared (by hand, on
    this dev database; see the commit's own report on why that does not
    scale to a database holding verified rows) before this applies, and a
    fresh clone has never had any rows to begin with.
    """

    dependencies = [
        ("statutory", "0011_termination_notice_band"),
    ]

    operations = [
        migrations.AddField(
            model_name="terminationnoticeband",
            name="service_from_inclusive",
            field=models.BooleanField(
                help_text=(
                    "Does this band include a service length of exactly service_from? "
                    "True for every band except one whose lower boundary the band below "
                    "already claims inclusively."
                )
            ),
        ),
        migrations.AddField(
            model_name="terminationnoticeband",
            name="service_to_inclusive",
            field=models.BooleanField(
                blank=True,
                help_text=(
                    "NULL for the open-ended top band. Read from the statute's own "
                    "wording."
                ),
                null=True,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="terminationnoticeband",
            name="termination_notice_band_service_to_paired",
        ),
        migrations.AddConstraint(
            model_name="terminationnoticeband",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("service_to_inclusive__isnull", True),
                        ("service_to_unit", ""),
                        ("service_to_value__isnull", True),
                    ),
                    models.Q(
                        ("service_to_inclusive__isnull", False),
                        ("service_to_unit__in", ["days", "weeks", "months", "years"]),
                        ("service_to_value__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="termination_notice_band_service_to_paired",
            ),
        ),
    ]
