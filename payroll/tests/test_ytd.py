"""``ytd_accumulator`` is a CACHE, and these are the tests that keep it one.

Invariant 3: year-to-date figures come from finalised payslips, the cache can be
discarded and rebuilt from source rows, and nothing ever "fixes" a total by
writing to it. P5 settled the same question for ``timesheet_summary`` (D-153),
and both halves of that lesson are here: the rebuild always recomputes from
scratch, and the staleness check is ground truth that never consults the cache's
own bookkeeping.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from payroll import ytd
from payroll.models import YtdAccumulator
from payroll.tests.conftest import a_line, a_payslip, a_period, a_run

pytestmark = pytest.mark.django_db


def a_finalised_payslip_with(household, tax_year, component, *, number, lines):
    """One finalised payslip in its own period, carrying the given lines."""
    period = a_period(
        household,
        tax_year,
        number=number,
        start=datetime.date(2026, 3, 1) + datetime.timedelta(days=28 * (number - 1)),
    )
    payslip = a_payslip(household, a_run(household, period), finalised=True)
    for source_code, amount in lines:
        a_line(
            household,
            payslip,
            component,
            source_code=source_code,
            amount_exact=Decimal(amount),
            amount=Decimal(amount).quantize(Decimal("0.01")),
        )
    return payslip


# ----------------------------------------------------------------- rebuilding


def test_the_cache_is_built_from_the_finalised_payslip_lines(household, tax_year, basic_component):
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=2, lines=[("3601", "5200.000000")]
    )

    rows = ytd.rebuild(household["employee"], tax_year)

    assert [(row.source_code, row.amount) for row in rows] == [("3601", Decimal("10200.00"))]
    assert rows[0].payslip_count == 2


def test_a_draft_payslip_is_not_year_to_date_anything(household, tax_year, basic_component):
    """A draft is a working figure that will change before anyone is paid.
    Counting it makes the total move under the employer's feet."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    draft = a_payslip(
        household,
        a_run(household, a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1))),
    )
    a_line(
        household,
        draft,
        basic_component,
        amount_exact=Decimal("9999.000000"),
        amount=Decimal("9999.00"),
    )

    rows = ytd.rebuild(household["employee"], tax_year)

    assert rows[0].amount == Decimal("5000.00")


def test_a_reversal_nets_off_because_its_lines_are_negative(household, tax_year, basic_component):
    """A reversing payslip is finalised like any other and its lines are the
    negation of the original. Netting them is precisely how a correction reaches
    the IRP5 — excluding reversals would leave the year-to-date overstated for
    ever."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=2, lines=[("3601", "-5000.000000")]
    )

    rows = ytd.rebuild(household["employee"], tax_year)

    assert rows[0].amount == Decimal("0.00")


def test_two_components_sharing_a_source_code_land_on_one_row(household, tax_year, basic_component):
    """BASIC, SUNDAY_2_0 and PH_WORKED are all 3601, and the IRP5 states one
    figure for the code. Keying the cache on the component would split them."""
    a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=1,
        lines=[("3601", "5000.000000"), ("3601", "810.000000"), ("3607", "135.000000")],
    )

    rows = ytd.rebuild(household["employee"], tax_year)

    assert [(row.source_code, row.amount) for row in rows] == [
        ("3601", Decimal("5810.00")),
        ("3607", Decimal("135.00")),
    ]


def test_rebuilding_discards_a_row_the_source_can_no_longer_produce(
    household, tax_year, basic_component
):
    """The reason ``rebuild()`` deletes before it writes. Updating in place is
    how a cache comes to carry a figure nothing in the source can produce — here,
    a source code whose only payslip was reversed out of existence."""
    payslip = a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3607", "135.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)

    with tenant_context(household["tenant"].pk):
        assert YtdAccumulator.objects.filter(source_code="3607").exists()
        payslip.lines.all().delete()

    ytd.rebuild(household["employee"], tax_year)

    with tenant_context(household["tenant"].pk):
        assert not YtdAccumulator.objects.filter(source_code="3607").exists()


def test_a_rebuild_is_idempotent(household, tax_year, basic_component):
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )

    first = ytd.rebuild(household["employee"], tax_year)
    second = ytd.rebuild(household["employee"], tax_year)

    assert [(r.source_code, r.amount) for r in first] == [(r.source_code, r.amount) for r in second]
    with tenant_context(household["tenant"].pk):
        assert YtdAccumulator.objects.count() == 1


# ------------------------------------------------------------- the ground truth


def test_a_cache_that_agrees_with_the_payslips_reports_no_disagreement(
    household, tax_year, basic_component
):
    """The half that proves the check is a check and not a wall — watched NOT
    firing before it is watched firing (D-153)."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)

    assert ytd.disagreements_with_source(household["employee"], tax_year) == []


def test_a_cache_edited_by_hand_is_caught_without_consulting_its_own_bookkeeping(
    household, tax_year, basic_component
):
    """``rebuilt_at`` is untouched here, so a check that trusted it would report
    the cache fresh and correct. This one compares figures."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)

    with tenant_context(household["tenant"].pk):
        YtdAccumulator.objects.filter(source_code="3601").update(amount=Decimal("1.00"))

    found = ytd.disagreements_with_source(household["employee"], tax_year)

    assert len(found) == 1
    assert found[0].cached_amount == Decimal("1.00")
    assert found[0].actual_amount == Decimal("5000.00")
    assert "cache says 1.00" in str(found[0])


def test_a_payslip_finalised_after_the_last_rebuild_shows_as_a_disagreement(
    household, tax_year, basic_component
):
    """The ordinary staleness case, and the one the flag-free check exists for:
    nothing about the cache row changed, the world did."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=2, lines=[("3601", "5200.000000")]
    )

    found = ytd.disagreements_with_source(household["employee"], tax_year)

    assert [f.actual_amount for f in found] == [Decimal("10200.00")]


def test_a_source_code_missing_from_the_cache_entirely_is_a_disagreement(
    household, tax_year, basic_component
):
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )

    found = ytd.disagreements_with_source(household["employee"], tax_year)

    assert found[0].cached_amount == Decimal("0")
    assert found[0].actual_amount == Decimal("5000.00")


# --------------------------------------------------------------------- reading


def test_the_total_reads_the_cache_and_can_be_narrowed_to_source_codes(
    household, tax_year, basic_component
):
    a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=1,
        lines=[("3601", "5000.000000"), ("3607", "135.000000")],
    )
    ytd.rebuild(household["employee"], tax_year)

    assert ytd.total_for(household["employee"], tax_year) == Decimal("5135.00")
    assert ytd.total_for(household["employee"], tax_year, source_codes=["3607"]) == Decimal(
        "135.00"
    )


def test_an_employee_with_nothing_finalised_totals_nil_rather_than_none(household, tax_year):
    assert ytd.total_for(household["employee"], tax_year) == Decimal("0")


def test_the_payslips_a_figure_came_from_can_be_listed(household, tax_year, basic_component):
    """Invariant 3's other half: the cache can always be traced back to the rows
    it was built from, by whoever is asking why a number is what it is."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    a_payslip(
        household,
        a_run(household, a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1))),
    )

    listed = ytd.finalised_payslips(household["employee"], tax_year)

    assert len(listed) == 1, "the draft is not a source of anything"
