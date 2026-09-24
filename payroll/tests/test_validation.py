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
    return [issue.issue_code for issue in issues]


# --------------------------------------------------------------- the P2 gate


def test_unverified_reference_data_blocks_the_run(household, tax_year):
    """The gate this whole build has been describing since P2. Nothing is
    verified on this database — 0 of 21 versions — so a run refuses, and that is
    the gate working rather than the gate failing."""
    a_version(verified=False)
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    assert "reference_data_not_verified" in codes(issues)
    issue = next(i for i in issues if i.issue_code == "reference_data_not_verified")
    assert issue.severity == PayrollValidationIssue.Severity.BLOCKING
    assert "SECOND person" in issue.message
    assert "verifystatutory" in issue.message


def test_verified_reference_data_does_not_block(household, tax_year, approver):
    """The other half: watched NOT firing before it is watched firing."""
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    assert "reference_data_not_verified" not in codes(issues)


def test_a_version_that_has_stopped_applying_is_not_demanded(household, tax_year, approver):
    """D-278. The 2023 BCCCI agreement applies from 1 April 2023 and every row
    it loaded closes on 1 April 2026, so a run in this period can read nothing
    from it — and the gate was naming four versions of it among the things
    somebody must go and verify before anyone is paid.

    A refusal that lists work nobody can do is one people learn to read past,
    which is D-271's lesson one command along. Watched firing first, so the
    exclusion is doing the work and not the test's own setup.
    """
    a_version(user=approver)
    expired = ReferenceDataVersion.objects.create(
        version_label="REF-2023.04.01-EXPIRED",
        applies_from=datetime.date(2023, 4, 1),
        description="An instrument that has been replaced",
    )
    period = a_period(household, tax_year)
    run = a_run(household, period)

    assert "reference_data_not_verified" in codes(validation.validate(run)), (
        "while it is open-ended it IS demanded, correctly"
    )

    # It stopped on the day this run pays, so the run can read nothing from it.
    expired.applies_until = period.payment_date
    expired.save(update_fields=["applies_until"])

    assert "reference_data_not_verified" not in codes(validation.validate(run))


def test_a_version_that_stops_applying_later_is_still_demanded(household, tax_year, approver):
    """The other side of the boundary, because "has an end date" is not the
    same as "has ended" — an instrument that runs out next year still governs
    this period and still has to be checked."""
    a_version(user=approver)
    ReferenceDataVersion.objects.create(
        version_label="REF-2026.01.01-STILL-RUNNING",
        applies_from=datetime.date(2026, 1, 1),
        description="Ends after this period",
        applies_until=datetime.date(2027, 1, 1),
    )
    run = a_run(household, a_period(household, tax_year))

    assert "reference_data_not_verified" in codes(validation.validate(run))


def test_no_reference_data_at_all_blocks_and_counts_what_is_loaded(household, tax_year):
    run = a_run(household, a_period(household, tax_year))

    issues = validation.validate(run)

    message = next(i for i in issues if i.issue_code == "reference_data_not_verified").message
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
    assert (
        "28 March 2026" in next(i for i in issues if i.issue_code == "reference_data_stale").message
    )


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
        household,
        run,
        total_earnings=Decimal("5000.00"),
        total_deductions=Decimal("0.00"),
        net_pay=Decimal("5000.00"),
    )
    a_line(
        household,
        payslip,
        basic_component,
        amount_unrounded=Decimal("4000.000000"),
        amount=Decimal("4000.00"),
    )

    issues = validation.validate(run)

    assert "totals_do_not_match_lines" in codes(issues)
    issue = next(i for i in issues if i.issue_code == "totals_do_not_match_lines")
    assert issue.employee_id == household["employee"].pk


def test_a_payslip_that_adds_up_raises_nothing(household, tax_year, approver, basic_component):
    a_version(user=approver)
    run = a_run(household, a_period(household, tax_year))
    payslip = a_payslip(
        household,
        run,
        total_earnings=Decimal("5000.00"),
        total_deductions=Decimal("0.00"),
        net_pay=Decimal("5000.00"),
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
        household,
        run,
        total_earnings=Decimal("5000.00"),
        total_deductions=Decimal("0.00"),
        net_pay=Decimal("5000.00"),
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
        household,
        run,
        total_earnings=Decimal("5000.00"),
        total_deductions=Decimal("0.00"),
        net_pay=Decimal("5000.00"),
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
    issue = next(i for i in issues if i.issue_code == "reference_data_not_verified")

    validation.resolve_issue(issue, resolved_by=approver, reason="Verified out of band, ref #77")
    validation.validate(run)

    with tenant_context(household["tenant"].pk):
        stored = PayrollValidationIssue.objects.get(pk=issue.pk)
    assert stored.acknowledged_by_user_id == approver.pk
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
                issue_code="made_up",
                severity=PayrollValidationIssue.Severity.WARNING,
                message="Something",
                acknowledged_at=WHEN,
            )

    assert "validation_issue_resolution_is_named_and_reasoned" in str(raised.value)


def test_an_issue_must_say_what_is_wrong(household, tax_year):
    run = a_run(household, a_period(household, tax_year))

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with tenant_context(household["tenant"].pk):
            PayrollValidationIssue.objects.create(
                tenant=household["tenant"],
                payroll_run=run,
                issue_code="made_up",
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


# ------ P2 chunk C: one version verified is not "the reference data verified"


def test_a_later_verified_version_does_not_vouch_for_an_earlier_unverified_one(
    household, tax_year, approver
):
    """THE HOLE CHUNK C's VERIFICATION OPENED (D-250).

    ``in_force_on()`` returns THE NEWEST usable version and nothing else, which
    was right when this build had one monolithic version and is wrong now that
    it has eighteen. Verifying the four BCCCI versions — KwaZulu-Natal wage
    rates, rule sets, notice bands and two maternity benefits, all applying from
    1 April 2026 — makes one of them the newest usable version, and the gate
    then passes for a June 2026 run whose PAYE, UIF and National Minimum Wage
    figures sit in REF-2026.03.01 with nobody's name against them.

    The gate went from refusing to passing because a KwaZulu-Natal wage schedule
    was checked. It must refuse while ANY version applying on that date is
    unverified, not merely find one that is.
    """
    ReferenceDataVersion.objects.create(
        version_label="REF-2026.03.01",
        applies_from=datetime.date(2026, 3, 1),
        description="PAYE, UIF and the NMW. Loaded, reconciles, NOBODY HAS CHECKED IT.",
    )
    ReferenceDataVersion.objects.create(
        version_label="REF-2026.04.01-BCCCI",
        applies_from=datetime.date(2026, 4, 1),
        description="KwaZulu-Natal contract cleaning wage rates.",
        verified_at=WHEN,
        verified_by_user=approver,
        golden_tests_passed=True,
        data_current_through=datetime.date(2029, 2, 28),
    )

    # June 2026 - AFTER the BCCCI applies from, which is what makes it the
    # newest usable version and hides the unverified one behind it.
    period = a_period(household, tax_year, start=datetime.date(2026, 6, 1))
    run = a_run(household, period)
    issues = validation.validate(run)

    assert "reference_data_not_verified" in codes(issues), (
        "the run reads PAYE from REF-2026.03.01, which nobody has verified; a verified "
        "BCCCI wage schedule does not vouch for it"
    )
    message = next(i for i in issues if i.issue_code == "reference_data_not_verified").message
    assert "REF-2026.03.01" in message, "the refusal must name the version that is missing"


def test_the_gate_passes_only_when_every_applicable_version_is_verified(
    household, tax_year, approver
):
    """The other direction, so the fix cannot be "always refuse"."""
    for label, applies in (
        ("REF-2026.03.01", datetime.date(2026, 3, 1)),
        ("REF-2026.04.01-BCCCI", datetime.date(2026, 4, 1)),
    ):
        ReferenceDataVersion.objects.create(
            version_label=label,
            applies_from=applies,
            description="Checked.",
            verified_at=WHEN,
            verified_by_user=approver,
            golden_tests_passed=True,
            data_current_through=datetime.date(2029, 2, 28),
        )

    period = a_period(household, tax_year, start=datetime.date(2026, 6, 1))
    run = a_run(household, period)
    issues = validation.validate(run)

    assert "reference_data_not_verified" not in codes(issues)


def test_a_superseded_version_is_not_demanded_of_the_verifier(household, tax_year, approver):
    """A re-encoded version is kept forever (D-199) and must not hold the gate
    shut for the rest of time — its successor is what anything reads."""
    old = ReferenceDataVersion.objects.create(
        version_label="REF-2026.04.01-BCCCI-RULES",
        applies_from=datetime.date(2026, 4, 1),
        description="The first encoding. Never verified; superseded.",
    )
    new = ReferenceDataVersion.objects.create(
        version_label="REF-2026.04.01-BCCCI-RULES-r2",
        applies_from=datetime.date(2026, 4, 1),
        description="Corrected.",
        verified_at=WHEN,
        verified_by_user=approver,
        golden_tests_passed=True,
        data_current_through=datetime.date(2029, 2, 28),
    )
    new.supersedes = old
    new.supersede_reason = "Re-encoded with the corrected effective date (D-247)."
    new.save(update_fields=["supersedes", "supersede_reason"])

    ReferenceDataVersion.objects.create(
        version_label="REF-2026.03.01",
        applies_from=datetime.date(2026, 3, 1),
        description="Checked.",
        verified_at=WHEN,
        verified_by_user=approver,
        golden_tests_passed=True,
        data_current_through=datetime.date(2029, 2, 28),
    )

    period = a_period(household, tax_year, start=datetime.date(2026, 6, 1))
    run = a_run(household, period)
    issues = validation.validate(run)

    assert "reference_data_not_verified" not in codes(issues)


# ----------- a development identity must not satisfy the gate for a real run


MACHINE = "claude-verification@labourmax.invalid"


def a_verified_version(label, applies, *, user, current_through=datetime.date(2029, 2, 28)):
    return ReferenceDataVersion.objects.create(
        version_label=label,
        applies_from=applies,
        description="Checked.",
        verified_at=WHEN,
        verified_by_user=user,
        golden_tests_passed=True,
        data_current_through=current_through,
    )


def test_a_machine_verified_version_does_not_satisfy_the_gate(household, tax_year, approver):
    """THE SHAPE THIS WILL ACTUALLY OCCUR IN (D-262). Kobus verifies the
    fourteen outstanding versions under his own name; four were verified by
    claude-verification@labourmax.invalid during development and ride along
    silently. The gate goes green and a real payslip is backed in part by data
    no person ever read.

    A development identity is recognised by RFC 2606's reserved .invalid suffix,
    which can never be a deliverable address, so nothing that reaches this
    branch was ever a person.
    """
    machine = get_user_model().objects.create_user(email=MACHINE, password="x" * 14)

    for index in range(14):
        a_verified_version(f"REF-HUMAN-{index:02d}", datetime.date(2026, 3, 1), user=approver)
    for index in range(4):
        a_verified_version(f"REF-MACHINE-{index:02d}", datetime.date(2026, 4, 1), user=machine)

    period = a_period(household, tax_year, start=datetime.date(2026, 6, 1))
    issues = validation.validate(a_run(household, period))

    assert "reference_data_not_verified" in codes(issues), (
        "four machine-verified versions must block the run exactly as unverified ones do"
    )
    message = next(i for i in issues if i.issue_code == "reference_data_not_verified").message
    for index in range(4):
        assert f"REF-MACHINE-{index:02d}" in message
    assert "REF-HUMAN-00" not in message, "a human-verified version is not the problem"
    assert "verifystatutory" in message
    assert "NOT A PERSON" in message
    assert MACHINE in message, "name the identity, or nobody knows which tick to redo"


def test_the_same_versions_verified_by_a_person_do_satisfy_the_gate(household, tax_year, approver):
    """Watched NOT firing, or the test above proves only that the gate is shut."""
    for index in range(14):
        a_verified_version(f"REF-HUMAN-{index:02d}", datetime.date(2026, 3, 1), user=approver)
    for index in range(4):
        a_verified_version(f"REF-WAS-MACHINE-{index}", datetime.date(2026, 4, 1), user=approver)

    period = a_period(household, tax_year, start=datetime.date(2026, 6, 1))
    issues = validation.validate(a_run(household, period))

    assert "reference_data_not_verified" not in codes(issues)


def test_a_machine_verified_version_is_not_in_force(household, tax_year, approver):
    """in_force_on() is the other door into the same room. If it still answered
    a machine-verified version, the staleness half of the gate would read that
    version's own data_current_through and the run would pass on it."""
    machine = get_user_model().objects.create_user(email=MACHINE, password="x" * 14)
    a_verified_version("REF-MACHINE-ONLY", datetime.date(2026, 3, 1), user=machine)

    assert ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1)) is None
