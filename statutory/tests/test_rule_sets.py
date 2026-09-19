"""The three rule sets, and the reason none of their columns has a default.

``leave_rule_set``, ``working_time_rule_set`` and ``termination_rule_set`` are what
the leave, working-time and termination engines read instead of carrying BCEA numbers
in code. One row per sector per period, with a NULL sector meaning the BCEA default
that applies where a sectoral determination is silent.

Two things are asserted here that are easy to lose later:

**No column has a database default.** The workbook specifies BCEA defaults on these
columns — 15 annual leave days, a 1.5 overtime multiplier. A column default is a
statutory figure living in a migration, and it is worse than an ordinary hard-coded
constant, because a row created without that field arrives in the database looking
exactly like a verified one, carrying a citation that never covered the value. The
test below walks the model registry, so a field added in 2028 with a helpful-looking
default fails immediately.

**The BCEA row cannot overlap itself.** Its ``sector`` is NULL, NULL is not equal to
NULL in PostgreSQL, and without ``COALESCE(sector, 0)`` in the exclusion constraint
the default rows would be the only ones in the table permitted to overlap — which is
the opposite of what anyone would assume.

The values used below are placeholders. Not one statutory figure appears in this file.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from statutory.models import (
    LeaveRuleSet,
    Sector,
    SectorRuleSet,
    TerminationRuleSet,
    WorkingTimeRuleSet,
)

MARCH_2026 = datetime.date(2026, 3, 1)
MARCH_2027 = datetime.date(2027, 3, 1)

CITATION = "Test fixture, not a real determination"

RULE_SETS = [LeaveRuleSet, WorkingTimeRuleSet, TerminationRuleSet]


def placeholder_for(field):
    """A type-appropriate value that is obviously not a statutory figure.

    Deliberately generated rather than listed: a rule set has upwards of twenty
    columns, and a hand-written fixture is a list that silently stops covering the
    model the first time a column is added.
    """
    if field.choices:
        # A column with choices now also carries a CHECK (the Conventions
        # table's own rule), so "x" is no longer a value the database accepts.
        return field.choices[0][0]
    internal = field.get_internal_type()
    if internal in {"DecimalField"}:
        return 1
    if internal in {"SmallIntegerField", "IntegerField", "BigIntegerField"}:
        return 1
    if internal == "BooleanField":
        return True
    if internal == "TimeField":
        return datetime.time(0, 0)
    if internal == "DateField":
        return MARCH_2026
    return "x"


def own_fields(model):
    """The fields declared on the rule set itself, not the inherited scaffolding."""
    return [
        f
        for f in model._meta.local_fields
        if f.concrete
        and not f.primary_key
        and f.name
        not in {
            "sector",
            # Scoped on area since the BCCCI Main Agreement (D-240). Like
            # ``sector`` it is part of the row's SCOPE rather than its content,
            # so the generic placeholder must not invent one.
            "sector_area",
            "effective_from",
            "effective_to",
            "source_reference",
            "source_url",
            "notes",
            "created_at",
            "updated_at",
            "created_by_user",
            "updated_by_user",
        }
    ]


def rule_set(model, *, sector=None, frm=MARCH_2026, to=None, **overrides):
    values = {f.name: placeholder_for(f) for f in own_fields(model)}
    values.setdefault("source_reference", CITATION)
    values.update(overrides)
    return model.objects.create(
        sector=sector,
        effective_from=frm,
        effective_to=to,
        **values,
    )


@pytest.fixture
def domestic(db):
    return Sector.objects.create(
        code=Sector.Code.DOMESTIC,
        name="Domestic worker sector",
        determination_reference="Sectoral Determination 7",
    )


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING,
        name="Contract cleaning sector",
        determination_reference="Sectoral Determination 1",
        uses_area_rates=True,
        has_statutory_annual_bonus=True,
    )


# ------------------------------------------------- no statutory figure in a default


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_no_rule_set_column_carries_a_default(model):
    """A default here would be a BCEA figure hard-coded into a migration.

    It is the worst place for one: a row created without the field looks exactly
    like a verified row, and carries a citation that never covered the value.
    """
    with_defaults = [f.name for f in own_fields(model) if f.has_default()]
    assert not with_defaults, (
        f"{model.__name__} has database defaults on {with_defaults}. Every statutory "
        f"value is loaded explicitly with a citation - see the note above "
        f"SectorRuleSet in statutory/models.py."
    )


@pytest.mark.statutory
def test_the_three_rule_sets_are_discovered():
    """Guards the tests above against a rename quietly emptying the parametrisation."""
    found = {m.__name__ for m in SectorRuleSet.__subclasses__()}
    assert found == {"LeaveRuleSet", "WorkingTimeRuleSet", "TerminationRuleSet"}


# ------------------------------------------------------------------- the citation


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_a_rule_set_without_a_citation_is_refused(db, model):
    with pytest.raises(IntegrityError), transaction.atomic():
        rule_set(model, source_reference="")


# ---------------------------------------------------------------------- overlaps


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_overlapping_periods_for_one_sector_are_refused(db, model, domestic):
    rule_set(model, sector=domestic, frm=MARCH_2026, to=MARCH_2027)
    with pytest.raises(IntegrityError), transaction.atomic():
        rule_set(model, sector=domestic, frm=datetime.date(2026, 9, 1), to=MARCH_2027)


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_two_bcea_default_rows_cannot_overlap(db, model):
    """The NULL-is-not-NULL trap, on the rows most likely to be loaded twice."""
    rule_set(model, sector=None, frm=MARCH_2026, to=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        rule_set(model, sector=None, frm=MARCH_2027, to=None)


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_a_sector_row_and_the_bcea_row_may_share_a_period(db, model, domestic):
    """They must: the sector row is the override, the BCEA row is the fallback."""
    assert rule_set(model, sector=None, frm=MARCH_2026).pk
    assert rule_set(model, sector=domestic, frm=MARCH_2026).pk


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_two_sectors_may_share_a_period(db, model, domestic, cleaning):
    assert rule_set(model, sector=domestic, frm=MARCH_2026).pk
    assert rule_set(model, sector=cleaning, frm=MARCH_2026).pk


@pytest.mark.statutory
@pytest.mark.parametrize("model", RULE_SETS, ids=[m.__name__ for m in RULE_SETS])
def test_consecutive_periods_are_allowed(db, model, domestic):
    rule_set(model, sector=domestic, frm=MARCH_2026, to=MARCH_2027)
    assert rule_set(model, sector=domestic, frm=MARCH_2027).pk


# --------------------------------------------------------- the value constraints


@pytest.mark.statutory
def test_an_overtime_multiplier_below_one_is_refused(db, domestic):
    """Overtime that pays less than ordinary time is a transcription error."""
    with pytest.raises(IntegrityError), transaction.atomic():
        rule_set(WorkingTimeRuleSet, sector=domestic, overtime_multiplier=0)


@pytest.mark.statutory
def test_a_bonus_month_outside_the_calendar_is_refused(db, cleaning):
    with pytest.raises(IntegrityError), transaction.atomic():
        rule_set(TerminationRuleSet, sector=cleaning, annual_bonus_month=13)


# ------------------------------------------- the night allowance pairing (D-113)


@pytest.mark.statutory
def test_a_zero_night_allowance_with_a_percentage_type_is_refused(db, domestic):
    """THE ROW NOBODY WOULD NOTICE WAS WRONG.

    ``night_allowance_value`` is NUMERIC, so BCEA s17(2) - which requires night work
    to be compensated and deliberately names no amount - is recorded as zero with a
    type of ``by_agreement`` beside it. Zero therefore means two different things and
    only the type column tells them apart: "the statute names no figure" against "the
    gazetted figure is nil".

    A row claiming ``percentage`` with a value of zero tells any calculator reading it
    literally that night work is compensated at nothing. It looks like data rather
    than like a mistake, and this check is the only thing between it and a payslip.

    Raised in the first verification pass, where the zero was marked wrong before the
    pair was agreed to say it already (D-113). Keeping the NUMERIC column was the
    decision; this is what stops the ambiguity becoming an underpayment.
    """
    from statutory import checks

    rule_set(
        WorkingTimeRuleSet,
        sector=domestic,
        night_allowance_type="percentage",
        night_allowance_value=Decimal(0),
    )

    issues = checks.check_night_allowance_coherence()
    assert [issue for issue in issues if issue.blocking], "A promised figure of zero must block."
    assert "pays nothing for night work" in " ".join(issue.message for issue in issues)


@pytest.mark.statutory
def test_by_agreement_carries_no_figure_at_all_and_a_zero_is_refused(db, domestic):
    """D-113 settled that the type column carried the meaning and the zero was a
    placeholder. O-22 reopened it and set the deadline at the start of P7, because
    the moment a calculator multiplies by that placeholder it pays nothing. The
    absence of a figure is now NULL, and the database refuses the zero.
    """
    from statutory import checks

    rule_set(
        WorkingTimeRuleSet,
        sector=domestic,
        night_allowance_type="by_agreement",
        night_allowance_value=None,
    )
    assert checks.check_night_allowance_coherence() == []

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        rule_set(
            WorkingTimeRuleSet,
            sector=domestic,
            frm=MARCH_2027,
            night_allowance_type="by_agreement",
            night_allowance_value=Decimal(0),
        )
    assert "working_time_night_allowance_without_a_figure_is_null" in str(raised.value)


@pytest.mark.statutory
def test_by_agreement_carrying_a_figure_is_refused(db, domestic):
    """The mirror image, and the likelier accident.

    SD1 gazettes ten percent for contract cleaning, so that sector carries a real
    figure with a real type. A value sitting behind ``by_agreement`` means one column
    was edited and the other was not - and the value is the one that gets used.
    """
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        rule_set(
            WorkingTimeRuleSet,
            sector=domestic,
            night_allowance_type="by_agreement",
            night_allowance_value=Decimal("10.0000"),
        )
    assert "working_time_night_allowance_without_a_figure_is_null" in str(raised.value)
