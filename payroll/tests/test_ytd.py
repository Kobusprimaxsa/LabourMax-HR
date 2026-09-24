"""``ytd_accumulator`` is a CACHE, and these are the tests that keep it one.

Invariant 3: year-to-date figures come from finalised payslips, the cache can be
discarded and rebuilt from source rows, and nothing ever "fixes" a total by
writing to it. P5 settled the same question for ``timesheet_summary`` (D-153),
and both halves of that lesson are here: the rebuild always recomputes from
scratch, and the staleness check is ground truth that never consults the cache's
own bookkeeping.

The row is sheet 02's shape since D-290: one per employee, tax year and
employer, with named totals and the per-code figures in ``ytd_by_source_code``.
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

CENT = Decimal("0.01")


def a_finalised_payslip_with(household, tax_year, component, *, number, lines, **header):
    """One finalised payslip in its own period, carrying the given lines. The
    header's earnings follow the lines, so the payslip is internally consistent;
    anything else on the header is passed in."""
    period = a_period(
        household,
        tax_year,
        number=number,
        start=datetime.date(2026, 3, 1) + datetime.timedelta(days=28 * (number - 1)),
    )
    earned = sum((Decimal(amount) for _, amount in lines), Decimal("0")).quantize(CENT)
    deductions = header.pop("total_deductions", Decimal("0.00"))
    payslip = a_payslip(
        household,
        a_run(household, period),
        finalised=True,
        total_earnings=earned,
        gross_remuneration=earned,
        total_deductions=deductions,
        net_pay=earned - deductions,
        is_reversal=header.pop("is_reversal", False),
        **header,
    )
    for source_code, amount in lines:
        a_line(
            household,
            payslip,
            component,
            source_code=source_code,
            amount_unrounded=Decimal(amount),
            amount=Decimal(amount).quantize(CENT),
        )
    return payslip


def cached(household, tax_year) -> YtdAccumulator | None:
    with tenant_context(household["tenant"].pk):
        return YtdAccumulator.objects.filter(
            employee=household["employee"], tax_year=tax_year
        ).first()


# ----------------------------------------------------------------- rebuilding


def test_the_cache_is_one_row_built_from_the_finalised_payslips(
    household, tax_year, basic_component
):
    a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=1,
        lines=[("3601", "5000.000000")],
        taxable_remuneration=Decimal("5000.00"),
        paye=Decimal("120.00"),
        uif_employee=Decimal("50.00"),
        uif_employer=Decimal("50.00"),
        uif_remuneration=Decimal("5000.00"),
        total_deductions=Decimal("170.00"),
    )
    a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=2,
        lines=[("3601", "5200.000000")],
        taxable_remuneration=Decimal("5200.00"),
        paye=Decimal("150.00"),
        uif_employee=Decimal("52.00"),
        uif_employer=Decimal("52.00"),
        uif_remuneration=Decimal("5200.00"),
        total_deductions=Decimal("202.00"),
    )

    (row,) = ytd.rebuild(household["employee"], tax_year)

    assert row.employer_id == household["employer"].pk
    assert row.periods_processed == 2
    assert row.ytd_gross == Decimal("10200.00")
    assert row.ytd_taxable == Decimal("10200.00")
    assert row.ytd_paye == Decimal("270.00")
    assert (row.ytd_uif_employee, row.ytd_uif_employer) == (Decimal("102.00"), Decimal("102.00"))
    assert row.ytd_uif_remuneration == Decimal("10200.00")
    assert row.ytd_by_source_code == {"3601": "10200.00"}
    assert row.last_payroll_run_id is not None


def test_the_coida_figure_is_capped_through_the_one_place_that_caps_it(
    household, tax_year, basic_component
):
    """R720 000 of COIDA earnings in the year is declared at the R668 000
    ceiling the household fixture loads — by ``payroll/coida.py``, not by a
    second copy of the rule here (D-285)."""
    for month in range(1, 13):
        a_finalised_payslip_with(
            household, tax_year, basic_component, number=month, lines=[("3601", "60000.000000")]
        )

    (row,) = ytd.rebuild(household["employee"], tax_year)

    assert row.ytd_gross == Decimal("720000.00")
    assert row.ytd_coida_remuneration == Decimal("668000.00")


def test_a_draft_payslip_is_not_year_to_date_anything(household, tax_year, basic_component):
    """A draft is a working figure that will change before anyone is paid."""
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
        amount_unrounded=Decimal("9999.000000"),
        amount=Decimal("9999.00"),
    )

    (row,) = ytd.rebuild(household["employee"], tax_year)

    assert row.ytd_gross == Decimal("5000.00")
    assert row.ytd_by_source_code == {"3601": "5000.00"}
    assert row.periods_processed == 1


def test_a_reversal_nets_off_and_its_period_stops_counting(household, tax_year, basic_component):
    """A reversing payslip is finalised like any other and its figures are the
    negation of the original. Netting them is how a correction reaches the IRP5;
    and a period reversed and not re-paid is not a period processed."""
    original = a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=2,
        lines=[("3601", "-5000.000000")],
        is_reversal=True,
        reverses_payslip=original,
    )

    (row,) = ytd.rebuild(household["employee"], tax_year)

    assert row.ytd_gross == Decimal("0.00")
    assert row.ytd_by_source_code == {"3601": "0.00"}
    assert row.periods_processed == 0


def test_components_sharing_a_source_code_land_on_one_key(household, tax_year, basic_component):
    """BASIC, SUNDAY_2_0 and PH_WORKED are all 3601, and the IRP5 states one
    figure for the code."""
    a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=1,
        lines=[("3601", "5000.000000"), ("3601", "810.000000"), ("3607", "135.000000")],
    )

    (row,) = ytd.rebuild(household["employee"], tax_year)

    assert row.ytd_by_source_code == {"3601": "5810.00", "3607": "135.00"}


def test_rebuilding_discards_a_code_the_source_can_no_longer_produce(
    household, tax_year, basic_component
):
    """Rebuilding from scratch is why a code whose only line is gone leaves
    nothing behind. Updating in place is how a cache comes to carry a figure
    nothing in the source can produce."""
    payslip = a_finalised_payslip_with(
        household,
        tax_year,
        basic_component,
        number=1,
        lines=[("3601", "5000.000000"), ("3607", "135.000000")],
    )
    ytd.rebuild(household["employee"], tax_year)
    assert "3607" in cached(household, tax_year).ytd_by_source_code

    with tenant_context(household["tenant"].pk):
        payslip.lines.filter(source_code="3607").delete()
    ytd.rebuild(household["employee"], tax_year)

    assert "3607" not in cached(household, tax_year).ytd_by_source_code


def test_a_rebuild_is_idempotent(household, tax_year, basic_component):
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )

    (first,) = ytd.rebuild(household["employee"], tax_year)
    (second,) = ytd.rebuild(household["employee"], tax_year)

    assert (first.ytd_gross, first.ytd_by_source_code) == (
        second.ytd_gross,
        second.ytd_by_source_code,
    )
    with tenant_context(household["tenant"].pk):
        assert YtdAccumulator.objects.count() == 1


def test_nothing_finalised_leaves_no_row(household, tax_year):
    assert ytd.rebuild(household["employee"], tax_year) == []
    assert cached(household, tax_year) is None


# ------------------------------------------------------------- the ground truth


def test_a_cache_that_agrees_with_the_payslips_reports_no_disagreement(
    household, tax_year, basic_component
):
    """The half that proves the check is a check and not a wall (D-153)."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)

    assert ytd.disagreements_with_source(household["employee"], tax_year) == []


def test_a_cache_edited_by_hand_is_caught_without_consulting_its_own_bookkeeping(
    household, tax_year, basic_component
):
    """``recalculated_at`` is untouched here, so a check that trusted it would
    report the cache fresh. This one compares figures — a named total and a
    per-code figure both."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)

    with tenant_context(household["tenant"].pk):
        YtdAccumulator.objects.update(
            ytd_gross=Decimal("1.00"), ytd_by_source_code={"3601": "1.00"}
        )

    found = {f.figure: f for f in ytd.disagreements_with_source(household["employee"], tax_year)}

    assert set(found) == {"ytd_gross", "3601"}
    assert found["ytd_gross"].cached_amount == Decimal("1.00")
    assert found["3601"].actual_amount == Decimal("5000.00")
    assert "cache says 1.00" in str(found["ytd_gross"])


def test_a_payslip_finalised_after_the_last_rebuild_shows_as_a_disagreement(
    household, tax_year, basic_component
):
    """The ordinary staleness case: nothing about the cache row changed, the
    world did."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=2, lines=[("3601", "5200.000000")]
    )

    found = {f.figure: f for f in ytd.disagreements_with_source(household["employee"], tax_year)}

    assert found["ytd_gross"].actual_amount == Decimal("10200.00")
    assert found["3601"].actual_amount == Decimal("10200.00")


def test_a_cache_that_was_never_built_is_a_disagreement(household, tax_year, basic_component):
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )

    found = {f.figure: f for f in ytd.disagreements_with_source(household["employee"], tax_year)}

    assert found["3601"].cached_amount == Decimal("0")
    assert found["3601"].actual_amount == Decimal("5000.00")


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
    it was built from."""
    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    a_payslip(
        household,
        a_run(household, a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1))),
    )

    listed = ytd.finalised_payslips(household["employee"], tax_year)

    assert len(listed) == 1, "the draft is not a source of anything"


def test_one_row_per_employee_year_and_employer_is_enforced(household, tax_year, basic_component):
    """PROVE EVERY GUARD FAILS: sheet 03's UNIQUE (employee_id, tax_year_id,
    employer_id), watched refusing a second row."""
    from django.db import IntegrityError, transaction

    a_finalised_payslip_with(
        household, tax_year, basic_component, number=1, lines=[("3601", "5000.000000")]
    )
    ytd.rebuild(household["employee"], tax_year)

    with pytest.raises(IntegrityError, match="uniq_ytd_per_employee_year_employer"):
        with transaction.atomic(), tenant_context(household["tenant"].pk):
            YtdAccumulator.objects.create(
                tenant=household["tenant"],
                employee=household["employee"],
                tax_year=tax_year,
                employer=household["employer"],
            )
