"""O-22, the half whose deadline was the start of P7: the night allowance pair.

``night_allowance_value = 0.0000`` stood in for "this instrument states no
amount" — the BCEA's and SD7's actual position, since s17(2)(a) requires an
allowance without setting one. The moment P7 prices night work that zero also
reads as "pay nothing", and the two are indistinguishable.

Unlike the accommodation cap (D-198), a type column already existed to tell them
apart — but it carried no choices and no CHECK, so it could hold any string at
all, including ``percentage`` beside a zero. So this migration does both halves:
the value becomes nullable and every row is mapped explicitly, and the type
column gets the CHECK the Conventions table has always required of an enum.

Every existing row is mapped from what its own instrument says, and a row this
cannot map unambiguously stops the migration rather than being guessed at —
guessing is how the sentinel got here.
"""

from django.db import migrations, models

WITH_A_FIGURE = ("percentage", "fixed_amount")
WITHOUT_A_FIGURE = ("time_off", "by_agreement")


def to_pair(apps, schema_editor):
    """A stated figure stays; `by_agreement` with a zero becomes NULL."""
    rule_set = apps.get_model("statutory", "WorkingTimeRuleSet")
    unmappable = []
    for row in rule_set.objects.select_related("sector").all():
        kind = row.night_allowance_type
        value = row.night_allowance_value
        if kind in WITH_A_FIGURE and value is not None and value > 0:
            continue
        if kind in WITHOUT_A_FIGURE and (value is None or value == 0):
            row.night_allowance_value = None
            row.save(update_fields=["night_allowance_value"])
            continue
        unmappable.append(
            f"  - working_time_rule_set {row.pk}: "
            f"sector {(row.sector.code if row.sector_id else 'BCEA default')}, "
            f"night_allowance_type {kind!r}, night_allowance_value {value}, "
            f"from {row.source_reference}"
        )
    if unmappable:
        raise RuntimeError(
            f"Cannot re-encode the night allowance for {len(unmappable)} "
            "working_time_rule_set row(s) without guessing. A type stating a figure "
            "(percentage, fixed_amount) must carry one; time_off and by_agreement must "
            "carry none. Read the instrument each row cites and set both columns "
            "explicitly, then migrate again:\n" + "\n".join(unmappable)
        )


def to_sentinel(apps, schema_editor):
    rule_set = apps.get_model("statutory", "WorkingTimeRuleSet")
    for row in rule_set.objects.filter(night_allowance_value__isnull=True):
        row.night_allowance_value = 0
        row.save(update_fields=["night_allowance_value"])


class Migration(migrations.Migration):
    dependencies = [("statutory", "0016_parental_leave_quantum_and_adoption_age_limit")]

    operations = [
        migrations.AlterField(
            model_name="workingtimeruleset",
            name="night_allowance_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=10,
                null=True,
                help_text=(
                    "Percent of the hourly wage, or rand per shift. NULL for time_off and "
                    "by_agreement — the absence of a figure, never a zero standing in for it."
                ),
            ),
        ),
        migrations.RunPython(to_pair, to_sentinel),
        migrations.AlterField(
            model_name="workingtimeruleset",
            name="night_allowance_type",
            field=models.CharField(
                choices=[
                    ("percentage", "A percentage of the hourly wage"),
                    ("fixed_amount", "A rand amount per shift"),
                    ("time_off", "Reduced working hours instead of an allowance"),
                    ("by_agreement", "The instrument states none — the parties agree it"),
                ],
                max_length=20,
                help_text=(
                    "Whether the instrument states a percentage, a rand amount, time off, or "
                    "nothing at all. No default: a row that does not say fails."
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="workingtimeruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    night_allowance_type__in=[
                        "percentage",
                        "fixed_amount",
                        "time_off",
                        "by_agreement",
                    ]
                ),
                name="working_time_night_allowance_type_is_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="workingtimeruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(_negated=True, night_allowance_type__in=WITH_A_FIGURE)
                | models.Q(night_allowance_value__isnull=False),
                name="working_time_night_allowance_states_its_figure",
            ),
        ),
        migrations.AddConstraint(
            model_name="workingtimeruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(night_allowance_type__in=WITH_A_FIGURE)
                | models.Q(night_allowance_value__isnull=True),
                name="working_time_night_allowance_without_a_figure_is_null",
            ),
        ),
    ]
