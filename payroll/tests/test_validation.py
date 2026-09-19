"""The validation gate, and the P2 rule that had never had a caller.

``ReferenceDataVersion.in_force_on()`` has filtered on ``verified_at`` since
P2's own migration, and ``data_current_through`` has carried the words "THE
STALENESS GUARD" just as long. Nothing called either, because there was no
payroll run to call them in. These are the tests that make the intention a
refusal.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from core.managers import tenant_context
from payroll import validation
from payroll.models import PayrollValidationIssue
from payroll.tests.conftest import a_line, a_payslip, a_period, a_run
from statutory.models import ReferenceDataVersion

pytestmark = pytest.mark.django_db

WHEN = datetime.datetime(2026, 3, 28, 10, 0, tzinfo=datetime.UTC)


@pytest.fixture
def approver(db):
    return get_user_model().objects.create_user(
        email="owner@example.com", password="x" * 14, first_name="Kobus", last_name="Owner"
    )


def a_version(*, verified=True, current_through=datetime.date(2027, 2, 28), user=None):
    return ReferenceDataVersion.objects.create(
        version_label="REF-2026.03.01",
        applies_from=datetime.date(2026, 3, 1),
        description="Test version",
        verified_at=WHEN if verified else None,
        verified_by_user=user if verified else None,
        golden_tests_passed=verified,
        data_current_through=current_through if verified else None,
    )


def codes(issues) -> list[str]:
    return [issue.code for issue in issues]


# --------------------------------------------------------------- the P2 gate


def test_unverified_reference_data_blocks_the_run(household, tax_year):
    """The gate this whole build has been describing since P2. Nothing is
    verified on this database — 0 of 21 versions — so a run refuses, and that is
    the gate working rather than the gate failing."""
    a_version(verified=False)
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    assert "reference_data_not_verified" in codes(issues)
    issue = next(i for i in issues if i.code == "reference_data_not_verified")
    assert issue.severity == PayrollValidationIssue.Severity.BLOCKING
    assert "SECOND person" in issue.message
    assert "verifystatutory" in issue.message


def test_verified_reference_data_does_not_block(household, tax_year, approver):
    """The other half: watched NOT firing before it is watched firing."""
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    assert "reference_data_not_verified" not in codes(issues)


def test_no_reference_data_at_all_blocks_and_counts_what_is_loaded(household, tax_year):
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    message = next(i for i in issues if i.code == "reference_data_not_verified").message
    assert "0 version(s) are loaded" in message


def test_a_period_beyond_what_the_data_is_confirmed_through_is_blocked(
    household, tax_year, approver
):
    """``data_current_through``'s own words, from the P2 migration: "A payroll
    run whose period ends after this is blocked rather than computed against
    figures nobody has checked." The UIF ceiling moves on ministerial notice
    with no fixed calendar, which is why this is not a tax-year question."""
    a_version(user=approver, current_through=datetime.date(2026, 3, 15))
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    assert "reference_data_stale" in codes(issues)
    assert "28 March 2026" in next(i for i in issues if i.code == "reference_data_stale").message


def test_a_period_ending_exactly_on_the_confirmed_date_is_not_stale(household, tax_year, approver):
    """Both sides of the boundary. The data is confirmed THROUGH that date, so
    a period ending on it is covered."""
    period = a_period(household, tax_year)
    a_version(user=approver, current_through=period.period_end)
    run = a_run(household, period)

    assert "reference_data_stale" not in codes(validation.validate(run))


# ----------------------------------------------------------- the other checks


def test_a_run_with_no_payslips_is_blocked(household, tax_year, approver):
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))

    assert "no_payslips" in codes(validation.validate(run))


def test_a_payslip_whose_total_is_not_its_lines_is_blocked(
    household, tax_year, approver, basic_component
):
    """Invariant 6 at the run's level. The line-level CHECK proves each amount
    is its own exact figure rounded; this proves the document's bottom line is
    what the lines add up to."""
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))
    payslip = a_payslip(
        household, run, gross_earnings=Decimal("5000.00"), total_deductions=Decimal("0.00")
    )
    a_line(
        household,
        payslip,
        basic_component,
        amount_exact=Decimal("4000.000000"),
        amount=Decimal("4000.00"),
    )

    issues = validation.validate(run)

    assert "totals_do_not_match_lines" in codes(issues)
    issue = next(i for i in issues if i.code == "totals_do_not_match_lines")
    assert issue.employee_id == household["employee"].pk


def test_a_payslip_that_adds_up_raises_nothing(household, tax_year, approver, basic_component):
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))
    payslip = a_payslip(
        household, run, gross_earnings=Decimal("5000.00"), total_deductions=Decimal("0.00")
    )
    a_line(household, payslip, basic_component)

    assert "totals_do_not_match_lines" not in codes(validation.validate(run))


def test_the_attendance_completeness_hook_is_wired_up(
    household, tax_year, approver, basic_component
):
    """P5 wrote ``missing_attendance_days()`` and said "P7's validation gate
    will call this; it is not built here". This is that call. A salaried
    employee has nothing missing by construction, which is the direction P5
    settled deliberately — so this asserts the hook runs and stays quiet rather
    than inventing attendance to trip it."""
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))
    payslip = a_payslip(
        household, run, gross_earnings=Decimal("5000.00"), total_deductions=Decimal("0.00")
    )
    a_line(household, payslip, basic_component)

    assert validation.check_attendance_is_complete(run) == []
    assert "attendance_incomplete" not in codes(validation.validate(run))


# -------------------------------------------------- rewriting and resolving


def test_validating_twice_does_not_accumulate_duplicates(household, tax_year):
    run = a_run(household, a_period(household, tax_year))

    validation.validate(run)
    second = validation.validate(run)

    with tenant_context(household["tenant"].pk):
        assert run.validation_issues.count() == len(second)


def test_a_problem_that_has_been_fixed_stops_being_reported(
    household, tax_year, approver, basic_component
):
    """The reason the list is rewritten rather than appended to: an issue that
    lingers after the thing it described was fixed is one nothing ever clears."""
    run = a_run(household, a_period(household, tax_year))
    validation.validate(run)
    assert "reference_data_not_verified" in codes(validation.blocking_issues(run))

    a_version(user=approver)
    payslip = a_payslip(
        household, run, gross_earnings=Decimal("5000.00"), total_deductions=Decimal("0.00")
    )
    a_line(household, payslip, basic_component)
    validation.validate(run)

    assert codes(validation.blocking_issues(run)) == []


def test_a_resolved_issue_survives_revalidation_and_stops_blocking(household, tax_year, approver):
    """Resolving is how an employer proceeds past a check they have looked at
    and accepted — D-108's own shape for a below-minimum wage. The decision is a
    record, so re-running the checks must not erase it."""
    run = a_run(household, a_period(household, tax_year))
    issues = validation.validate(run)
    issue = next(i for i in issues if i.code == "reference_data_not_verified")

    validation.resolve_issue(issue, resolved_by=approver, reason="Verified out of band, ref #77")
    validation.validate(run)

    with tenant_context(household["tenant"].pk):
        stored = PayrollValidationIssue.objects.get(pk=issue.pk)
    assert stored.resolved_by_user_id == approver.pk
    assert stored.resolution_reason == "Verified out of band, ref #77"
    assert issue.pk not in [i.pk for i in validation.blocking_issues(run)]


def test_resolving_without_a_reason_is_refused(household, tax_year, approver):
    run = a_run(household, a_period(household, tax_year))
    issue = validation.validate(run)[0]

    with pytest.raises(ValueError, match="needs a reason"):
        validation.resolve_issue(issue, resolved_by=approver, reason="   ")


def test_the_database_refuses_a_half_resolved_issue(household, tax_year, approver):
    """All three of when, who and why, or none of them. Guarded both ways over
    the nullable columns, because a CHECK that evaluates to NULL passes."""
    run = a_run(household, a_period(household, tax_year))

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with tenant_context(household["tenant"].pk):
            PayrollValidationIssue.objects.create(
                tenant=household["tenant"],
                payroll_run=run,
                code="made_up",
                severity=PayrollValidationIssue.Severity.WARNING,
                message="Something",
                resolved_at=WHEN,
            )

    assert "validation_issue_resolution_is_named_and_reasoned" in str(raised.value)


def test_an_issue_must_say_what_is_wrong(household, tax_year):
    run = a_run(household, a_period(household, tax_year))

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with tenant_context(household["tenant"].pk):
            PayrollValidationIssue.objects.create(
                tenant=household["tenant"],
                payroll_run=run,
                code="made_up",
                severity=PayrollValidationIssue.Severity.WARNING,
                message="",
            )

    assert "validation_issue_says_what_is_wrong" in str(raised.value)


def test_every_check_is_in_the_registry():
    """A check written and not registered is a check that never runs — which is
    this codebase's oldest failure mode, in a new place."""
    registered = {check.__name__ for check in validation.CHECKS}
    defined = {
        name
        for name in dir(validation)
        if name.startswith("check_") and callable(getattr(validation, name))
    }

    assert defined == registered, f"not registered: {sorted(defined - registered)}"
