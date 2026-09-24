"""The payslip, and the four things about it that are not conventions.

Invariant 4 (a finalised payslip is never rewritten), invariant 6 (the line is
the one place a figure is rounded), invariant 7 (a payslip freezes what it
showed) and the reversal pair are each held by the DATABASE, and each one is
watched refusing the case it exists for.

The assembly that would populate these rows is blocked on P2 verification and is
not built. That is why every payslip here is written by hand: there is nothing
yet that makes one, and these tests are about the table.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from core.db.rls import MAINTENANCE_VAR
from core.managers import tenant_context
from payroll.models import PayrollRun, Payslip, PayslipLine
from payroll.tests.conftest import a_line, a_payslip, a_period, a_run

pytestmark = pytest.mark.django_db

WHEN = datetime.datetime(2026, 3, 28, 10, 0, tzinfo=datetime.UTC)


# ------------------------------------------- invariant 4: finalised is frozen


def test_an_unfinalised_payslip_is_edited_freely(household, tax_year):
    """The other half of the guard, and the half that proves it is a guard
    rather than a wall: while the run is still being worked, a payslip changes
    as often as the calculators are re-run."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))

    with tenant_context(household["tenant"].pk):
        Payslip.objects.filter(pk=payslip.pk).update(
            total_deductions=Decimal("1000.00"), net_pay=Decimal("4000.00")
        )
        assert Payslip.objects.get(pk=payslip.pk).net_pay == Decimal("4000.00")


def test_a_finalised_payslip_cannot_be_updated(household, tax_year):
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)), finalised=True)

    with pytest.raises(DatabaseError) as raised, transaction.atomic():
        with tenant_context(household["tenant"].pk):
            Payslip.objects.filter(pk=payslip.pk).update(net_pay=Decimal("9999.00"))

    message = str(raised.value)
    assert "is finalised and cannot be update" in message
    assert "REVERSING payslip plus a replacement" in message


def test_a_finalised_payslip_cannot_be_deleted(household, tax_year):
    """Unlike a locked attendance day, which a whole-run reversal removes along
    with everything else. The reversal of a payslip IS a new payslip, so the
    original has to survive to be reversed against."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)), finalised=True)

    with pytest.raises(DatabaseError), transaction.atomic():
        with tenant_context(household["tenant"].pk):
            Payslip.objects.filter(pk=payslip.pk).delete()

    with tenant_context(household["tenant"].pk):
        assert Payslip.objects.filter(pk=payslip.pk).exists()


def test_the_retention_purge_can_still_delete_through_the_named_hole(household, tax_year):
    """One named, greppable, logged exception rather than a permanently
    writable table — the same shape ``append_only()`` uses for P11's POPIA
    purge, which must be able to delete a record whose retention has expired."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)), finalised=True)

    with tenant_context(household["tenant"].pk), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT set_config('{MAINTENANCE_VAR}', 'on', true)")
        Payslip.objects.filter(pk=payslip.pk).delete()

    with tenant_context(household["tenant"].pk):
        assert not Payslip.objects.filter(pk=payslip.pk).exists()


def test_the_trigger_is_installed_on_the_table_itself():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'payslip'::regclass AND NOT tgisinternal"
        )
        triggers = {row[0] for row in cursor.fetchall()}
    assert "payslip_no_change_when_finalised" in triggers


# --------------------------------------------- invariant 6: rounding, once


def test_a_line_whose_amount_is_not_its_exact_figure_rounded_is_refused(
    household, tax_year, basic_component
):
    """Invariant 6 made structural. The unrounded figure and the rounded one are
    stored together, and nothing may write a pair that does not agree — which is
    how a payslip comes to show a total its own lines do not add up to."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_line(
            household,
            payslip,
            basic_component,
            amount_unrounded=Decimal("100.005000"),
            amount=Decimal("100.00"),
        )

    assert "payslip_line_amount_is_the_exact_figure_rounded" in str(raised.value)


@pytest.mark.parametrize(
    ("exact", "rounded"),
    [
        ("100.005000", "100.01"),
        ("100.004999", "100.00"),
        ("-100.005000", "-100.01"),
        ("5000.000000", "5000.00"),
    ],
)
def test_half_up_away_from_zero_is_what_the_database_agrees_to(
    household, tax_year, basic_component, exact, rounded
):
    """Python's ``ROUND_HALF_UP`` rounds a tie AWAY FROM ZERO, and so does
    PostgreSQL's ``round()`` on numeric. The negative case is the one that would
    expose a mismatch, so it is the one pinned here."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))

    line = a_line(
        household,
        payslip,
        basic_component,
        amount_unrounded=Decimal(exact),
        amount=Decimal(rounded),
    )

    assert line.pk


def test_a_line_must_name_its_component_in_frozen_text(household, tax_year, basic_component):
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_line(household, payslip, basic_component, component_code="")

    assert "payslip_line_names_its_component" in str(raised.value)


# ----------------------------------------- invariant 7: it freezes what it showed


def test_the_snapshot_survives_the_employee_changing_underneath_it(
    household, tax_year, basic_component
):
    """Reprinting a 2027 payslip after the employee married and changed banks
    must reproduce the ORIGINAL. Reading the name through the foreign key would
    reproduce today instead, and the reprint would differ from the document the
    employee was handed."""
    payslip = a_payslip(
        household,
        a_run(household, a_period(household, tax_year)),
        finalised=True,
        employee_snapshot={
            "full_name": "Thandi Mokoena",
            "employee_number": "EMP-0001",
            "position": "Domestic worker",
            "rate": "5000.00",
            "bank_reference": "****1234",
        },
    )

    employee = household["employee"]
    with tenant_context(household["tenant"].pk):
        employee.last_name = "Ndlovu"
        employee.save(update_fields=["last_name"])
        stored = Payslip.objects.get(pk=payslip.pk)
        # ``stored.employee`` reads as attribute access on a row already in
        # hand and is a QUERY — so it stays inside the pinned context, or RLS
        # returns nothing and it raises DoesNotExist. This test hit that on its
        # first run, which is the trap CLAUDE.md names and the reason it does.
        live_surname = stored.employee.last_name

    assert stored.employee_snapshot["full_name"] == "Thandi Mokoena"
    assert live_surname == "Ndlovu", "the live row moved; the snapshot did not"


# ------------------------------------------------------------- the CHECK pairs


def test_a_finalised_payslip_must_carry_its_timestamp(household, tax_year):
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_payslip(
            household,
            a_run(household, a_period(household, tax_year)),
            is_finalised=True,
        )

    assert "payslip_finalised_has_a_timestamp" in str(raised.value)


def test_an_unfinalised_payslip_may_not_carry_one(household, tax_year):
    """Both ways over the nullable column, because a CHECK that evaluates to
    NULL counts as SATISFIED (D-170) — the half nobody writes is the half that
    silently does nothing."""
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_payslip(household, a_run(household, a_period(household, tax_year)), finalised_at=WHEN)

    assert "payslip_finalised_has_a_timestamp" in str(raised.value)


def test_only_a_reversal_may_name_what_it_reverses(household, tax_year):
    run = a_run(household, a_period(household, tax_year))
    original = a_payslip(household, run, finalised=True)
    later = a_run(
        household,
        a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1)),
        number=1,
    )

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_payslip(household, later, reverses_payslip=original)

    assert "payslip_only_a_reversal_reverses_something" in str(raised.value)


def test_a_reversal_must_name_what_it_reverses(household, tax_year):
    later = a_run(
        household,
        a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1)),
        number=1,
    )

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_payslip(household, later, is_reversal=True)

    assert "payslip_a_reversal_names_what_it_reverses" in str(raised.value)


def test_a_reversal_pointing_at_the_original_is_accepted(household, tax_year):
    original = a_payslip(household, a_run(household, a_period(household, tax_year)), finalised=True)
    later = a_run(
        household,
        a_period(household, tax_year, number=2, start=datetime.date(2026, 4, 1)),
        number=1,
    )

    reversal = a_payslip(
        household,
        later,
        is_reversal=True,
        reverses_payslip=original,
        total_earnings=Decimal("-5000.00"),
        total_deductions=Decimal("-50.00"),
        net_pay=Decimal("-4950.00"),
    )

    assert reversal.reverses_payslip_id == original.pk


def test_two_payslips_for_one_employee_on_one_run_are_refused(household, tax_year):
    run = a_run(household, a_period(household, tax_year))
    a_payslip(household, run)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_payslip(household, run)

    assert "uniq_payslip_per_employee_per_run" in str(raised.value)


# --------------------------------------------------------------- payroll_run


def test_a_finalised_run_must_say_when_and_who(household, tax_year):
    period = a_period(household, tax_year)

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        a_run(household, period, status=PayrollRun.Status.FINALISED)

    assert "payroll_run_finalised_has_a_timestamp" in str(raised.value)


def test_the_run_status_check_does_not_claim_to_police_transitions(household, tax_year):
    """D-145's lesson, held as a test so nobody reads the CHECK as more than it
    is: a run goes from draft straight to approved here, skipping calculating
    and calculated, because the CHECK proves the VALUE is known and never that
    the MOVE was legal. The transition function is assembly and is not built."""
    run = a_run(household, a_period(household, tax_year))

    with tenant_context(household["tenant"].pk):
        PayrollRun.objects.filter(pk=run.pk).update(
            status=PayrollRun.Status.APPROVED, approved_at=WHEN
        )
        assert PayrollRun.objects.get(pk=run.pk).status == PayrollRun.Status.APPROVED


# ------------------------------------------------- the trace's forward reference


def test_the_calculation_trace_now_points_at_a_real_payslip(household, tax_year):
    """D-208 said ``payslip_id_ref`` would become a real foreign key in the
    chunk that built the payslip. This is that chunk."""
    from payroll.models import PayrollCalculationTrace

    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))

    with tenant_context(household["tenant"].pk):
        trace = PayrollCalculationTrace.objects.create(
            tenant=household["tenant"],
            employee=household["employee"],
            payslip=payslip,
            calculator_name="gross.gross_pay",
            calculated_for=datetime.date(2026, 3, 31),
            inputs={},
            outputs={},
        )
        assert trace.payslip_id == payslip.pk

    field = PayrollCalculationTrace._meta.get_field("payslip")
    assert field.is_relation and field.null


def test_lines_cascade_from_their_payslip_and_the_payslip_does_not(
    household, tax_year, basic_component
):
    """A line has no existence apart from its payslip, which is the one place a
    cascade is right here. The payslip itself is PROTECTed from its run."""
    payslip = a_payslip(household, a_run(household, a_period(household, tax_year)))
    a_line(household, payslip, basic_component)

    with tenant_context(household["tenant"].pk):
        Payslip.objects.filter(pk=payslip.pk).delete()
        assert not PayslipLine.objects.filter(payslip_id=payslip.pk).exists()
