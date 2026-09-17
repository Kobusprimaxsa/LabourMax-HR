"""D-198 amended: the accommodation cap becomes an explicit pair.

``accommodation_deduction_max_pct = 0.00`` used to stand in for "this instrument
states no cap". A value standing in for the absence of a value: a genuine zero
cap was unrepresentable and read as unlimited, and a new row whose author never
considered the column was silently uncapped.

The column is added NULLABLE, every existing row is mapped explicitly, and only
THEN is it made NOT NULL — so no blanket database default is ever written, and a
row this migration cannot map unambiguously stops it rather than being guessed at.
"""

from django.db import migrations, models


def to_pair(apps, schema_editor):
    """0.00 becomes capped=False/NULL; a real figure becomes capped=True.

    Refuses any row it cannot map from the mapping D-198's amendment records:
    SD7 capped at its stated percentage, the BCEA default and SD1 uncapped. A
    row outside that — an unknown sector, a domestic row with no figure, a BCEA
    or SD1 row carrying one — is a fact nobody has read, and guessing at it is
    how the sentinel caused this in the first place.
    """
    rule_set = apps.get_model("statutory", "WorkingTimeRuleSet")
    unmappable = []
    for row in rule_set.objects.select_related("sector").all():
        code = row.sector.code if row.sector_id else None
        pct = row.accommodation_deduction_max_pct
        if code == "DOMESTIC" and pct is not None and pct > 0:
            row.accommodation_deduction_capped = True
        elif code in (None, "CONTRACT_CLEANING") and pct == 0:
            row.accommodation_deduction_capped = False
            row.accommodation_deduction_max_pct = None
        else:
            unmappable.append(
                f"  - working_time_rule_set {row.pk}: sector {code or 'BCEA default'}, "
                f"accommodation_deduction_max_pct {pct}, from {row.source_reference}"
            )
            continue
        row.save(
            update_fields=["accommodation_deduction_capped", "accommodation_deduction_max_pct"]
        )
    if unmappable:
        raise RuntimeError(
            "Cannot re-encode the accommodation cap for "
            f"{len(unmappable)} working_time_rule_set row(s) without guessing. Read the "
            "instrument each cites and set accommodation_deduction_capped and "
            "accommodation_deduction_max_pct explicitly, then migrate again:\n"
            + "\n".join(unmappable)
        )


def to_sentinel(apps, schema_editor):
    rule_set = apps.get_model("statutory", "WorkingTimeRuleSet")
    for row in rule_set.objects.all():
        if not row.accommodation_deduction_capped:
            row.accommodation_deduction_max_pct = 0
            row.save(update_fields=["accommodation_deduction_max_pct"])


class Migration(migrations.Migration):
    dependencies = [("statutory", "0013_parameter_unit_months")]

    operations = [
        migrations.AddField(
            model_name="workingtimeruleset",
            name="accommodation_deduction_capped",
            field=models.BooleanField(null=True),
        ),
        migrations.AlterField(
            model_name="workingtimeruleset",
            name="accommodation_deduction_max_pct",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.RunPython(to_pair, to_sentinel),
        migrations.AlterField(
            model_name="workingtimeruleset",
            name="accommodation_deduction_capped",
            field=models.BooleanField(
                help_text=(
                    "Does this instrument cap the accommodation deduction? FALSE means it "
                    "states no percentage (the BCEA and SD1), not that nothing may be "
                    "deducted. No default: a row that does not say fails."
                )
            ),
        ),
        migrations.AlterField(
            model_name="workingtimeruleset",
            name="accommodation_deduction_max_pct",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text=(
                    "The cap, when capped. NULL when not. SD7 states 10 percent of the wage."
                ),
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="workingtimeruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(accommodation_deduction_capped=False)
                | models.Q(accommodation_deduction_max_pct__isnull=False),
                name="working_time_capped_states_its_percentage",
            ),
        ),
        migrations.AddConstraint(
            model_name="workingtimeruleset",
            constraint=models.CheckConstraint(
                condition=models.Q(accommodation_deduction_capped=True)
                | models.Q(accommodation_deduction_max_pct__isnull=True),
                name="working_time_uncapped_states_no_percentage",
            ),
        ),
    ]
