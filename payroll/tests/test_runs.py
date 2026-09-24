"""The payroll run's lifecycle: the state machine, the gate, finalisation and
the reversal that is the only correction.

Nothing here CALCULATES — that is the next chunk, and ``calculate()`` refuses by
name rather than producing empty payslips that would look like a successful run.
So every payslip in these tests is written by hand, exactly as chunk 6's are.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from attendance.models import AttendanceDay
from core.managers import tenant_context
from payroll import runs, validation, ytd
from payroll.models import PayPeriod, PayrollRun, Payslip, YtdAccumulator
from payroll.tests.conftest import a_line, a_payslip, a_period, a_run
from payroll.tests.test_validation import a_version

pytestmark = pytest.mark.django_db

Status = PayrollRun.Status


@pytest.fixture
def approver(db):
    return get_user_model().objects.create_user(
        email="owner@example.com", password="x" * 14, first_name="Kobus", last_name="Owner"
    )


def a_clean_run(household, tax_year, approver, basic_component, *, period=None):
    """A run with everything the gate asks for: verified data, a payslip whose
    total is its lines, and a salaried employee with nothing missing."""
    a_version(user=approver)
    period = period or a_period(household, tax_year)
    run = a_run(household, period)
    payslip = a_payslip(
        household,
        run,
        total_earnings=Decimal("5000.00"),
        total_deductions=Decimal("0.00"),
        net_pay=Decimal("5000.00"),
    )
    a_line(household, payslip, basic_component)
    return run


def to_calculated(run):
    runs.transition(run, Status.CALCULATING)
    return runs.transition(run, Status.CALCULATED)


# ----------------------------------------------------------- the state machine


def test_every_status_appears_in_the_transition_map():
    """A status missing from the map would be treated as terminal by accident,
    and a run would silently become unmovable."""
    assert set(runs.LEGAL_TRANSITIONS) == set(Status.values)


def test_a_legal_move_is_made(household, tax_year):
    run = a_run(household, a_period(household, tax_year))

    runs.transition(run, Status.CALCULATING)

    with tenant_context(household["tenant"].pk):
        assert PayrollRun.objects.get(pk=run.pk).status == Status.CALCULATING


def test_an_illegal_move_is_refused_and_names_where_it_could_go(household, tax_year):
    """Chunk 6 proved the CHECK does NOT police transitions. This is what does."""
    run = a_run(household, a_period(household, tax_year))

    with pytest.raises(runs.PayrollRunError) as raised:
        runs.transition(run, Status.FINALISED)

    message = str(raised.value)
    assert "cannot go from draft to finalised" in message
    assert "it may go to calculating" in message


def test_a_reversed_run_is_terminal(household, tax_year):
    run = a_run(household, a_period(household, tax_year), status=Status.REVERSED)

    with pytest.raises(runs.PayrollRunError, match="terminal"):
        runs.transition(run, Status.DRAFT)


def test_calculating_can_go_back_to_draft(household, tax_year):
    """A calculation abandoned halfway is not a dead run."""
    run = a_run(household, a_period(household, tax_year))
    runs.transition(run, Status.CALCULATING)

    assert runs.transition(run, Status.DRAFT).status == Status.DRAFT


# ------------------------------------------------------------------ opening


def test_the_first_run_over_a_period_is_run_one(household, tax_year):
    run = runs.open_run(a_period(household, tax_year))

    assert run.run_number == 1
    assert run.status == Status.DRAFT


def test_a_second_live_run_over_one_period_is_refused(household, tax_year):
    """Two live runs would each produce a payslip for the same employee for the
    same days, and nothing downstream could tell which was real."""
    period = a_period(household, tax_year)
    runs.open_run(period)

    with pytest.raises(runs.PayrollRunError, match="still draft"):
        runs.open_run(period)


def test_a_correction_run_follows_a_finalised_one(household, tax_year, approver, basic_component):
    period = a_period(household, tax_year)
    first = a_clean_run(household, tax_year, approver, basic_component, period=period)
    to_calculated(first)
    runs.approve(first, approved_by=approver)
    runs.finalise(first, finalised_by=approver)
    with tenant_context(household["tenant"].pk):
        period.refresh_from_db()
        period.status = PayPeriod.Status.REOPENED
        period.save(update_fields=["status"])

    second = runs.open_run(period)

    assert second.run_number == 2


def test_a_run_cannot_be_opened_over_a_closed_period(household, tax_year):
    period = a_period(household, tax_year)
    with tenant_context(household["tenant"].pk):
        period.status = PayPeriod.Status.CLOSED
        period.closed_at = datetime.datetime(2026, 3, 28, tzinfo=datetime.UTC)
        period.save(update_fields=["status", "closed_at"])

    with pytest.raises(runs.PayrollRunError, match="is closed"):
        runs.open_run(period)


# ---------------------------------------------------------------- calculating


def test_calculating_refuses_by_name_rather_than_producing_nothing(household, tax_year):
    """A run that reported itself calculated while holding no payslips is a run
    somebody approves. One guard deep is not deep enough for that."""
    run = a_run(household, a_period(household, tax_year))

    with pytest.raises(NotImplementedError) as raised:
        runs.calculate(run)

    assert "next chunk" in str(raised.value)


# ------------------------------------------------------------------ the gate


def test_approval_is_refused_while_a_blocking_issue_stands(household, tax_year, approver):
    """Nothing is verified, so the P2 gate blocks — which is the gate working."""
    run = to_calculated(a_run(household, a_period(household, tax_year)))

    with pytest.raises(runs.ApprovalRefusedError) as raised:
        runs.approve(run, approved_by=approver)

    assert "reference_data_not_verified" in str(raised.value)
    assert [i.issue_code for i in raised.value.issues]
    with tenant_context(household["tenant"].pk):
        assert PayrollRun.objects.get(pk=run.pk).status == Status.CALCULATED


def test_a_clean_run_is_approved_and_says_who_and_when(
    household, tax_year, approver, basic_component
):
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))

    approved = runs.approve(run, approved_by=approver)

    assert approved.status == Status.APPROVED
    assert approved.approved_by_user_id == approver.pk
    assert approved.approved_at is not None


def test_approval_revalidates_rather_than_trusting_an_earlier_pass(
    household, tax_year, approver, basic_component
):
    """The picture can move between validating and approving — attendance
    re-imported, a payslip re-calculated. An approval granted against a stale
    pass is exactly what the gate exists to prevent."""
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    assert validation.blocking_issues(run) == []

    with tenant_context(household["tenant"].pk):
        Payslip.objects.filter(payroll_run=run).update(
            total_earnings=Decimal("9999.00"), net_pay=Decimal("9999.00")
        )

    with pytest.raises(runs.ApprovalRefusedError, match="totals_do_not_match_lines"):
        runs.approve(run, approved_by=approver)


def test_a_resolved_blocking_issue_lets_the_run_through(household, tax_year, approver):
    """An employer who has looked at a check and accepted it must be able to
    proceed, with their name and reason on the row for ever."""
    run = to_calculated(a_run(household, a_period(household, tax_year)))
    for issue in validation.validate(run):
        validation.resolve_issue(issue, resolved_by=approver, reason="Accepted, ticket #12")

    # Re-validation rewrites the UNRESOLVED issues, and there are none left to
    # rewrite, so the resolved ones stand and nothing blocks.
    approved = runs.approve(run, approved_by=approver)

    assert approved.status == Status.APPROVED


# --------------------------------------------------------------- finalisation


def test_finalising_freezes_the_snapshot_the_payslips_and_the_period(
    household, tax_year, approver, basic_component
):
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)

    report = runs.finalise(run, finalised_by=approver)

    assert report.payslips_finalised == 1
    with tenant_context(household["tenant"].pk):
        payslip = Payslip.objects.get(payroll_run=run)
        period = PayPeriod.objects.get(pk=run.pay_period_id)
        stored_run = PayrollRun.objects.get(pk=run.pk)
    assert payslip.is_finalised and payslip.finalised_at is not None
    assert payslip.employee_snapshot["full_name"] == "Thandi Mokoena"
    assert payslip.employee_snapshot["position"] == "Domestic worker"
    assert payslip.employee_snapshot["as_at"] == period.period_end.isoformat()
    assert period.status == PayPeriod.Status.CLOSED
    assert stored_run.status == Status.FINALISED
    assert stored_run.finalised_by_user_id == approver.pk


def test_the_snapshot_is_read_at_the_periods_date_not_at_todays(
    household, tax_year, approver, basic_component
):
    """``current_pay_basis`` is a cache refreshed as at TODAY (D-107), and today
    is not the date this payslip is for. The snapshot reads the effective-dated
    rows at the period's own end date."""
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)

    runs.finalise(run, finalised_by=approver)

    with tenant_context(household["tenant"].pk):
        snapshot = Payslip.objects.get(payroll_run=run).employee_snapshot
    assert snapshot["as_at"] == "2026-03-28"
    assert snapshot["pay_basis"], "the remuneration row in force then, not the cache"


def test_finalising_rebuilds_the_year_to_date_cache(household, tax_year, approver, basic_component):
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)

    report = runs.finalise(run, finalised_by=approver)

    assert report.ytd_rows_rebuilt == 1
    assert ytd.total_for(household["employee"], tax_year) == Decimal("5000.00")
    assert ytd.disagreements_with_source(household["employee"], tax_year) == []


def test_finalising_locks_the_periods_attendance(household, tax_year, approver, basic_component):
    """Invariant 4 for the attendance behind the pay: a day that has been paid
    for is not edited afterwards."""
    with tenant_context(household["tenant"].pk):
        day = AttendanceDay.objects.create(
            tenant=household["tenant"],
            employee=household["employee"],
            work_date=datetime.date(2026, 3, 10),
            day_type=AttendanceDay.DayType.ORDINARY,
            status=AttendanceDay.Status.CAPTURED,
        )
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)

    report = runs.finalise(run, finalised_by=approver)

    assert report.attendance_days_locked == 1
    with tenant_context(household["tenant"].pk):
        day.refresh_from_db()
    assert day.status == AttendanceDay.Status.LOCKED
    assert day.locked_by_payroll_run_id_ref == run.pk


def test_a_finalised_run_cannot_be_approved_again(household, tax_year, approver, basic_component):
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)
    runs.finalise(run, finalised_by=approver)

    with pytest.raises(runs.PayrollRunError, match="cannot go from finalised to approved"):
        runs.approve(run, approved_by=approver)


# ------------------------------------------------------------------ reversal


def test_only_a_finalised_run_can_be_reversed(household, tax_year, approver, basic_component):
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))

    with pytest.raises(runs.PayrollRunError, match="Only a finalised run is reversed"):
        runs.reverse(run, reversed_by=approver, reason="wrong")


def test_reversing_needs_a_reason(household, tax_year, approver, basic_component):
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)
    runs.finalise(run, finalised_by=approver)

    with pytest.raises(runs.PayrollRunError, match="needs a reason"):
        runs.reverse(run, reversed_by=approver, reason="  ")


def test_a_reversal_mirrors_every_payslip_and_leaves_the_original_alone(
    household, tax_year, approver, basic_component
):
    """Invariant 4. Nothing about the original moves — it cannot, the trigger
    sees to that — and the correction is a new run whose payslips are the
    negation of the originals."""
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)
    runs.finalise(run, finalised_by=approver)

    reversal = runs.reverse(run, reversed_by=approver, reason="Wrong hours imported")

    with tenant_context(household["tenant"].pk):
        original_slip = Payslip.objects.get(payroll_run=run)
        mirror = Payslip.objects.get(payroll_run=reversal)
        mirror_lines = list(mirror.lines.all())
        stored_run = PayrollRun.objects.get(pk=run.pk)

    assert stored_run.status == Status.REVERSED
    assert reversal.status == Status.FINALISED
    assert reversal.reverses_run_id == run.pk
    assert original_slip.total_earnings == Decimal("5000.00"), "untouched"
    assert mirror.is_reversal and mirror.reverses_payslip_id == original_slip.pk
    assert mirror.total_earnings == Decimal("-5000.00")
    assert [line.amount for line in mirror_lines] == [Decimal("-5000.00")]
    assert mirror_lines[0].description.startswith("Reversal:")


def test_a_reversal_nets_the_year_to_date_back_to_nil(
    household, tax_year, approver, basic_component
):
    """The reason a reversing payslip is finalised and counted rather than
    excluded: netting is how the correction reaches the IRP5."""
    run = to_calculated(a_clean_run(household, tax_year, approver, basic_component))
    runs.approve(run, approved_by=approver)
    runs.finalise(run, finalised_by=approver)
    assert ytd.total_for(household["employee"], tax_year) == Decimal("5000.00")

    runs.reverse(run, reversed_by=approver, reason="Wrong hours imported")

    assert ytd.total_for(household["employee"], tax_year) == Decimal("0.00")
    with tenant_context(household["tenant"].pk):
        assert YtdAccumulator.objects.filter(employee=household["employee"]).count() == 1
