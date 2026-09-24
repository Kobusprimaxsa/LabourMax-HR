"""O-28, closed as a standing check rather than a one-off reading (D-289).

The payroll tables were first built without sheet 02 in front of them. They are
now reconciled to it, and this file keeps them so: it reads the column
dictionary out of ``statutory-sources/Labourmax-HR_Database_Specification_3.xlsx``
and asserts every column sheet 02 names exists on the model — except the few
deviations named below, each with the decision that makes it one. A column
dropped, renamed away from the workbook, or never added fails here by name.

Then each constraint the reconciliation added is watched refusing the case it
exists for, message asserted (PROVE EVERY GUARD FAILS).
"""

from __future__ import annotations

import datetime
import pathlib
from decimal import Decimal

import openpyxl
import pytest
from django.db import DatabaseError, IntegrityError, transaction

from core.managers import tenant_context
from payroll import models as payroll_models
from payroll.models import PayrollRun, Payslip, PayslipLine
from payroll.tests.conftest import a_line, a_payslip, a_period, a_run

SPEC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "statutory-sources"
    / "Labourmax-HR_Database_Specification_3.xlsx"
)

MODELS = {
    "pay_period": payroll_models.PayPeriod,
    "payroll_run": payroll_models.PayrollRun,
    "payslip": payroll_models.Payslip,
    "payslip_line": payroll_models.PayslipLine,
    "ytd_accumulator": payroll_models.YtdAccumulator,
    "payroll_calculation_trace": payroll_models.PayrollCalculationTrace,
    "payroll_validation_issue": payroll_models.PayrollValidationIssue,
    "annual_bonus_cycle": payroll_models.AnnualBonusCycle,
}

#: Sheet 02 columns deliberately absent, and why. Anything not listed must exist.
DEVIATIONS = {
    # P8 (D-289): written AFTER finalisation, and the finalised-row trigger
    # (D-229) refuses every change to a finalised payslip. They arrive with
    # the payslip PDF and delivery, together with the trigger's exception.
    ("payslip", "pdf_file_id"),
    ("payslip", "delivered_at"),
    ("payslip", "delivery_channel"),
    ("payslip", "first_viewed_at"),
    ("payslip", "acknowledged_at"),
}


def sheet_02_columns() -> dict[str, list[str]]:
    workbook = openpyxl.load_workbook(SPEC, read_only=True, data_only=True)
    columns: dict[str, list[str]] = {}
    for row in workbook["02 Column Dictionary"].iter_rows(values_only=True):
        if row and len(row) > 3 and row[1] in MODELS and row[3]:
            columns.setdefault(row[1], []).append(str(row[3]))
    return columns


def test_the_specification_is_where_this_test_reads_it():
    assert SPEC.exists(), f"{SPEC} is the source of truth for every model (CLAUDE.md)."
    assert set(sheet_02_columns()) == set(MODELS)


@pytest.mark.parametrize("table", sorted(MODELS))
def test_every_sheet_02_column_exists_on_the_model(table):
    model = MODELS[table]
    have = {field.column for field in model._meta.concrete_fields}
    missing = [
        column
        for column in sheet_02_columns()[table]
        if column not in have and (table, column) not in DEVIATIONS
    ]
    assert not missing, f"{table} lacks sheet 02 column(s) {missing}"


def test_a_listed_deviation_is_still_actually_absent():
    """A deviation that has since been built should come off the list, or the
    list stops saying what is true."""
    for table, column in DEVIATIONS:
        have = {field.column for field in MODELS[table]._meta.concrete_fields}
        assert column not in have, f"{table}.{column} exists now; remove it from DEVIATIONS"


# ------------------------------------------------------------- the constraints


def refused(tenant, match, write):
    with pytest.raises((IntegrityError, DatabaseError), match=match):
        with transaction.atomic(), tenant_context(tenant.pk):
            write()


@pytest.mark.django_db
def test_net_pay_must_be_earnings_less_deductions(household, tax_year):
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))
    refused(
        household["tenant"],
        "payslip_net_is_earnings_less_deductions",
        lambda: Payslip.objects.filter(pk=payslip.pk).update(net_pay=Decimal("1.00")),
    )


@pytest.mark.django_db
def test_an_ordinary_payslip_may_not_store_a_negative_net(household, tax_year):
    """Sheet 03: "negative net triggers a blocking validation, not a stored
    negative". A reversal is exempt — its net is the negation of a positive."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))
    refused(
        household["tenant"],
        "payslip_net_not_negative",
        lambda: Payslip.objects.filter(pk=payslip.pk).update(
            total_deductions=Decimal("6000.00"), net_pay=Decimal("-1000.00")
        ),
    )


@pytest.mark.django_db
def test_a_payslip_number_is_unique_within_a_tenant(household, tax_year):
    first = a_payslip(household, a_run(household, a_period(household, tax_year)))
    later_run = a_run(
        household, a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1))
    )
    refused(
        household["tenant"],
        "uniq_payslip_number_per_tenant",
        lambda: a_payslip(household, later_run, payslip_number=first.payslip_number),
    )


@pytest.mark.django_db
def test_a_run_must_name_its_engine_version(household, tax_year):
    run = a_run(household, a_period(household, tax_year))
    refused(
        household["tenant"],
        "payroll_run_names_its_engine_version",
        lambda: PayrollRun.objects.filter(pk=run.pk).update(engine_version=""),
    )


@pytest.mark.django_db
def test_a_run_type_outside_sheet_04_is_refused(household, tax_year):
    run = a_run(household, a_period(household, tax_year))
    refused(
        household["tenant"],
        "payroll_run_type_is_known",
        lambda: PayrollRun.objects.filter(pk=run.pk).update(run_type="holiday"),
    )


@pytest.mark.django_db
def test_a_line_component_type_outside_sheet_02_is_refused(household, tax_year, basic_component):
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))
    line = a_line(household, payslip, basic_component)
    refused(
        household["tenant"],
        "payslip_line_component_type_is_known",
        lambda: PayslipLine.objects.filter(pk=line.pk).update(component_type="perk"),
    )


@pytest.mark.django_db
def test_a_finalised_runs_totals_cannot_change_but_its_status_can(household, tax_year):
    """Sheet 03: "No UPDATE of totals once status='finalised'". The reversal
    still has to move the run to 'reversed', so the trigger refuses totals and
    lets the status through."""
    from django.utils import timezone

    from core.models import AppUser

    who = AppUser.objects.create_user(email="finaliser@example.com", password="x" * 16)
    run = a_run(household, a_period(household, tax_year))
    with tenant_context(household["tenant"].pk):
        PayrollRun.objects.filter(pk=run.pk).update(
            status=PayrollRun.Status.FINALISED, finalised_at=timezone.now(), finalised_by_user=who
        )
    refused(
        household["tenant"],
        "totals of a finalised payroll run cannot change",
        lambda: PayrollRun.objects.filter(pk=run.pk).update(total_gross=Decimal("1.00")),
    )
    with tenant_context(household["tenant"].pk):
        PayrollRun.objects.filter(pk=run.pk).update(status=PayrollRun.Status.REVERSED)
        assert PayrollRun.objects.get(pk=run.pk).status == "reversed"
