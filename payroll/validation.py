"""The validation gate: what a payroll run must answer for before anyone is paid.

Every check is a function taking a run and returning ``Finding``s. ``validate()``
runs all of them, rewrites the run's unresolved issues from what they found, and
``payroll/runs.py::approve()`` refuses while any BLOCKING one stands.

**THE REFERENCE DATA CHECK IS THE P2 GATE, and this is the first thing that has
ever enforced it.** ``ReferenceDataVersion.in_force_on()`` has existed since
P2's own migration — it filters on ``verified_at`` and ``golden_tests_passed``,
so an unverified version is invisible to it — and ``data_current_through`` has
carried the words "THE STALENESS GUARD ... A payroll run whose period ends after
this is blocked rather than computed against figures nobody has checked" since
the same migration. Nothing called either, because there has never been a
payroll run to call them in. Every note in this build saying "the assembly is
blocked on P2 verification" was describing an intention; this module is where
the intention becomes a refusal.

So on this database today a run refuses, because REF-2026.03.01 — every PAYE
bracket, the UIF ceiling, the National Minimum Wage — has nobody's name against
it. That is not this module failing to work; it is this module working, and
there is a test that pins exactly that.

**The check asks about EVERY applicable version, not the newest one** (D-250).
Verifying P2 chunk C's four BCCCI versions — KwaZulu-Natal wage rates and
conditions, nothing else — made one of them the newest usable version and, under
the first version of this check, silently opened the gate for a June 2026 run
whose tax figures were still unchecked. The staleness half reads the SHORTEST
vouch across those versions for the same reason.

**Issues are DERIVED and rewritten, never accumulated** (D-231's shape, D-153's
reasoning). What survives a re-validation is a RESOLVED issue, because a
resolution is a human decision with a name and a reason on it.

**A negative leave balance is a WARNING and never a deduction** (D-185).
Recovering one is a BCEA s34 deduction requiring the employee's written consent,
so it is surfaced for a person to decide on and nothing here nets it off.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum

from attendance.completeness import missing_attendance_days
from core.managers import tenant_context_of
from leave.negative_balances import negative_balances
from payroll.models import PayrollValidationIssue
from statutory.models import ReferenceDataVersion

BLOCKING = PayrollValidationIssue.Severity.BLOCKING
WARNING = PayrollValidationIssue.Severity.WARNING


@dataclasses.dataclass(frozen=True)
class Finding:
    """One thing wrong, before it becomes a row."""

    code: str
    severity: str
    message: str
    #: The person it is about, or None for an issue about the run or the period.
    employee: object | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == BLOCKING


# ------------------------------------------------------------------ the checks


def check_reference_data(run) -> list[Finding]:
    """The P2 gate, finally with a caller.

    THREE separate failures, and they are not the same thing. There may be no
    usable version AT ALL for the period — nothing verified, or nothing applying
    that far back. There may be one whose author said, in
    ``data_current_through``, how far they were willing to vouch for it. Or —
    the one this build actually walked into (D-250) — there may be a verified
    version sitting in front of an UNVERIFIED one that the same run reads.

    ``in_force_on()`` answers "the newest usable version", which was the right
    question when this build had one monolithic version and is the wrong one now
    that it has eighteen. Verifying the four BCCCI versions, which carry nothing
    but KwaZulu-Natal wage rates and conditions, made one of them the newest
    usable version and stopped the gate asking about REF-2026.03.01 — where
    every PAYE bracket, the UIF ceiling and the National Minimum Wage live. The
    gate went from refusing to passing because a wage schedule for one province
    was checked.

    So the question is not "is there a verified version" but "is EVERY version
    this period could read verified". A superseded version is excluded: it is
    kept forever as the audit record (D-199) and nothing resolves against it, so
    demanding its verification would hold the gate shut permanently.

    A version verified by a DEVELOPMENT identity is unverified here, on this same
    code path and not on a second one beside it (D-262). Two lists that have to
    stay in step do not stay in step; ``ReferenceDataVersion.unusable_q()`` is
    the single expression, shared with ``in_force_on()``.
    """
    period = run.pay_period

    unverified = list(
        ReferenceDataVersion.objects.filter(
            applies_from__lte=period.payment_date,
            superseded_by__isnull=True,
        )
        # AND IT HAS NOT STOPPED APPLYING (D-278). ``applies_from`` alone made
        # every version that ever applied applicable forever: the 2023 BCCCI
        # agreement's rows all close on 1 April 2026, so a June 2026 run can
        # read none of them, and the gate was still naming four versions of it
        # among the things somebody must go and verify. Nobody can act on that,
        # and a refusal listing things nobody can act on is one people learn to
        # read past — D-271's lesson, one command along.
        .filter(
            Q(applies_until__isnull=True) | Q(applies_until__gt=period.payment_date),
        )
        .filter(ReferenceDataVersion.unusable_q())
        .select_related("verified_by_user")
        .order_by("applies_from", "version_label")
    )
    if unverified:
        machine = [v for v in unverified if v.is_machine_verified]
        named = ", ".join(v.version_label for v in unverified)
        machine_note = ""
        if machine:
            machine_note = (
                f" {len(machine)} of them carry a tick that is NOT A PERSON: "
                f"{', '.join(v.version_label for v in machine)}, verified by "
                f"{', '.join(sorted({v.verified_by_user.email for v in machine}))}. "
                f"A development identity gets a version past `verifystatutory` so the "
                f"rest of the build can be worked on; it does not get a payslip past "
                f"this gate, and it is listed above alongside the versions nobody "
                f"ticked at all because it means exactly the same thing here (D-262)."
            )
        return [
            Finding(
                "reference_data_not_verified",
                BLOCKING,
                f"{len(unverified)} reference data version(s) applying on "
                f"{period.payment_date:%d %B %Y} have not been verified: {named}. A "
                f"version is unusable until a SECOND person has checked every figure "
                f"against its source document and recorded it with "
                f"`manage.py verifystatutory`, including that the golden tests passed. "
                f"Every applicable version must be verified, not merely the newest one "
                f"— a run reads figures from all of them, so a verified wage schedule "
                f"does not vouch for an unverified tax table sitting behind it (D-250). "
                f"Running payroll against unchecked figures is the failure this gate "
                f"exists for.{machine_note}",
            )
        ]

    version = ReferenceDataVersion.in_force_on(period.payment_date)

    if version is None:
        loaded = ReferenceDataVersion.objects.filter(applies_from__lte=period.payment_date).count()
        return [
            Finding(
                "reference_data_not_verified",
                BLOCKING,
                f"No verified reference data applies on {period.payment_date:%d %B %Y}. "
                f"{loaded} version(s) are loaded for that date and none is usable: a "
                f"version is invisible until a SECOND person has checked every figure "
                f"against its source document and recorded it with "
                f"`manage.py verifystatutory`, including that the golden tests passed. "
                f"Running payroll against unchecked figures is the failure this gate "
                f"exists for.",
            )
        ]

    # The SHORTEST vouch wins, for the same reason the check above looks at every
    # version: the run reads figures from all of them, so the date it may safely
    # be computed for is the earliest any of their verifiers was willing to go.
    # Reading only the newest version's date would let a wage schedule vouched to
    # 2029 carry a tax table vouched to 2027.
    stalest = min(
        (
            candidate
            for candidate in ReferenceDataVersion.objects.filter(
                applies_from__lte=period.payment_date,
                superseded_by__isnull=True,
                data_current_through__isnull=False,
            )
        ),
        key=lambda candidate: candidate.data_current_through,
        default=version,
    )

    if stalest.data_current_through and period.period_end > stalest.data_current_through:
        version = stalest  # the one whose verifier vouched least far, and so the one to name
        return [
            Finding(
                "reference_data_stale",
                BLOCKING,
                f"This period ends on {period.period_end:%d %B %Y}, beyond the "
                f"{version.data_current_through:%d %B %Y} that {version.version_label} is "
                f"confirmed correct through. The figures may have moved — the UIF ceiling "
                f"changes on ministerial notice with no fixed calendar — and computing "
                f"against superseded rates silently is worse than refusing. Load the new "
                f"data and verify it.",
            )
        ]
    return []


def check_the_run_has_payslips(run) -> list[Finding]:
    """A run nobody can be paid from should not reach approval looking healthy."""
    if not run.payslips.exists():
        return [
            Finding(
                "no_payslips",
                BLOCKING,
                "This run has no payslips. Approving it would approve nothing, and "
                "finalising it would close the period with nobody paid.",
            )
        ]
    return []


def check_payslip_totals_match_their_lines(run) -> list[Finding]:
    """Invariant 6 at the run's own level.

    ``payslip_line`` already proves each line's rounded amount is its exact one
    rounded (D-230). This proves the payslip's totals are what its lines add up
    to — the other half, and the half an employee notices, because the total is
    the figure on the bottom of the document.
    """
    found = []
    for payslip in run.payslips.all():

        def summed(component_type, payslip=payslip):
            return payslip.lines.filter(component_type=component_type).aggregate(
                total=Sum("amount")
            )["total"] or Decimal("0")

        earned, deducted = summed("earning"), summed("deduction")
        contributed = summed("employer_contribution")
        if (
            earned != payslip.total_earnings
            or deducted != payslip.total_deductions
            or contributed != payslip.total_employer_contributions
        ):
            found.append(
                Finding(
                    "totals_do_not_match_lines",
                    BLOCKING,
                    f"This payslip states earnings {payslip.total_earnings}, deductions "
                    f"{payslip.total_deductions} and employer contributions "
                    f"{payslip.total_employer_contributions}; its lines add up to {earned}, "
                    f"{deducted} and {contributed}. A payslip whose totals are not its own "
                    f"lines is one nobody can explain to the employee holding it.",
                    employee=payslip.employee,
                )
            )
    return found


def check_employees_not_priced(run) -> list[Finding]:
    """Everybody the run owes a payslip and has not got one, and WHY (D-292).

    Derived like every other issue: the assembly is asked again, and its
    refusal is the message. So fixing the data and re-validating clears the
    issue, and nothing has to remember a refusal between two calls.
    """
    from payroll import assembly

    if run.status not in ("calculated", "approved"):
        return []
    paid = set(run.payslips.values_list("employee_id", flat=True))
    found = []
    for employee in assembly.employees_for(run):
        if employee.pk in paid:
            continue
        try:
            assembly.price(run, employee)
        except assembly.CannotPrice as refusal:
            found.append(
                Finding(
                    f"not_priced:{refusal.code}",
                    BLOCKING,
                    f"No payslip: {refusal}",
                    employee=employee,
                )
            )
        else:
            found.append(
                Finding(
                    "not_priced:stale",
                    BLOCKING,
                    "No payslip, and nothing now stops one being priced. Recalculate the run.",
                    employee=employee,
                )
            )
    return found


def check_payment_details(run) -> list[Finding]:
    """Sheet 02's NO_BANK_ACCOUNT: an EFT payslip with no account to pay into."""
    return [
        Finding(
            "no_bank_account",
            WARNING,
            "Paid by EFT and no bank account is in force for the period. Capture one, or "
            "record that this employee is paid in cash.",
            employee=payslip.employee,
        )
        for payslip in run.payslips.filter(payment_method="eft", bank_account__isnull=True)
    ]


def check_sdl_registration(run) -> list[Finding]:
    """SDL is levied only where an SDL registration is captured (D-209). No row
    reads as "not liable", which is right for a household and wrong for a
    contract cleaner over R500 000 — so it is said, once per run."""
    from employers.models import EmployerStatutoryRegistration

    if EmployerStatutoryRegistration.objects.filter(
        employer=run.employer,
        registration_type=EmployerStatutoryRegistration.RegistrationType.SDL,
    ).exists():
        return []
    return [
        Finding(
            "no_sdl_registration",
            WARNING,
            "No SDL registration is captured for this employer, so no levy was calculated. "
            "That is right below R500 000 of annual payroll (SDL Act s4(b)); above it, "
            "capture the registration and recalculate.",
        )
    ]


def check_attendance_is_complete(run) -> list[Finding]:
    """P5's own hook, wired up at last.

    ``attendance/completeness.py`` was written in P5 with its direction settled
    deliberately: it answers "what is missing" ONLY for an attendance-driven pay
    basis, because for a salaried employee no row means an ordinary day worked
    and flagging every uncaptured day would be noise. P5's notes say "P7's
    validation gate will call this; it is not built here." This is that call.
    """
    if run.run_type == "bonus":
        return []  # a bonus pays for no days (D-311); the regular run answers this
    found = []
    for payslip in run.payslips.select_related("employee"):
        missing = missing_attendance_days(payslip.employee, run.pay_period)
        if missing:
            found.append(
                Finding(
                    "attendance_incomplete",
                    BLOCKING,
                    f"{len(missing)} scheduled working day(s) in this period have no "
                    f"attendance captured, the first on {missing[0]:%d %B %Y}. This "
                    f"employee is paid from what was captured, so an uncaptured day is "
                    f"an unpaid one.",
                    employee=payslip.employee,
                )
            )
    return found


def check_overdrawn_leave(run) -> list[Finding]:
    """D-185, as a WARNING and never as a deduction.

    Recovering an overdrawn balance is a BCEA s34 deduction and needs the
    employee's written consent, so this surfaces the figure for a person to
    decide on. Nothing here nets it off anything, and the termination payout
    does not either.
    """
    employer = run.pay_period.pay_group.employer
    overdrawn = {
        balance.employee_id: balance
        for balance in negative_balances(employer, as_at=run.pay_period.period_end)
    }
    found = []
    for payslip in run.payslips.select_related("employee"):
        balance = overdrawn.get(payslip.employee_id)
        if balance is not None:
            found.append(
                Finding(
                    "leave_overdrawn",
                    WARNING,
                    f"This employee's leave balance is overdrawn by {balance.balance} "
                    f"{balance.unit}. It is NOT netted off pay: recovering it is a BCEA "
                    f"s34 deduction requiring the employee's written consent, which is a "
                    f"decision for a person to make.",
                    employee=payslip.employee,
                )
            )
    return found


def check_overdrawn_leave_at_termination(run) -> list[Finding]:
    """D-185 where it bites (D-309): a leaver's overdrawn leave on the FINAL payslip.

    ``check_overdrawn_leave`` reads OPEN cycles, and a leaver's are closed the
    day service ends (D-172), so it never sees the one employee for whom this
    is the last chance to decide. This reads the ended engagement's own cycles
    through ``leave/negative_balances.py::at_termination()``.

    BLOCKING, where the ordinary check warns: the payout is NOT reduced, and a
    named person must record what happens instead — recovered separately with
    the employee's written consent under s34, or written off — before the final
    payment goes. The resolution is that record (D-234).
    """
    from leave.negative_balances import at_termination

    found = []
    for payslip in run.payslips.filter(is_termination_payslip=True).select_related(
        "employee", "engagement"
    ):
        for item in at_termination(payslip.engagement):
            found.append(
                Finding(
                    "leave_overdrawn_at_termination",
                    BLOCKING,
                    f"{item.leave_type_code} leave for the cycle from "
                    f"{item.cycle.cycle_start:%d %B %Y} is overdrawn by {-item.balance} "
                    f"{item.unit}. It has NOT been netted off the final payment: recovering "
                    f"it is a BCEA s34 deduction needing the employee's written consent. "
                    f"Record the decision - recovered with consent, or written off - when "
                    f"resolving this.",
                    employee=payslip.employee,
                )
            )
    return found


#: Every check, in the order an employer would want to read them: the ones about
#: the run and its data first, then the ones about individual people.
CHECKS = (
    check_reference_data,
    check_the_run_has_payslips,
    check_payslip_totals_match_their_lines,
    check_employees_not_priced,
    check_payment_details,
    check_sdl_registration,
    check_attendance_is_complete,
    check_overdrawn_leave,
    check_overdrawn_leave_at_termination,
)


# ----------------------------------------------------------------- the gate


@transaction.atomic
def validate(run) -> list[PayrollValidationIssue]:
    """Run every check and rewrite this run's unresolved issues from the result.

    Deletes the unresolved ones first, so a problem somebody has FIXED stops
    being reported rather than lingering as a row nothing clears. Resolved
    issues survive untouched: that resolution is a record of a human decision,
    and rewriting it away would erase the only evidence the decision was made.
    """
    with tenant_context_of(run):
        found: list[Finding] = []
        for check in CHECKS:
            found.extend(check(run))

        run.validation_issues.filter(acknowledged_at__isnull=True).delete()

        # A resolution is against a PROBLEM, not against a row. The problem is
        # identified by (code, employee) on this run — so a finding somebody has
        # already looked at and accepted is not re-raised, which is the whole
        # point of being able to resolve one. Keying on the row id instead would
        # make every resolution last exactly until the next validation.
        already_resolved = set(
            run.validation_issues.filter(acknowledged_at__isnull=False).values_list(
                "issue_code", "employee_id"
            )
        )
        issues = [
            PayrollValidationIssue.objects.create(
                tenant=run.tenant,
                payroll_run=run,
                employee=finding.employee,
                issue_code=finding.code,
                severity=finding.severity,
                message=finding.message,
            )
            for finding in found
            if (finding.code, finding.employee.pk if finding.employee else None)
            not in already_resolved
        ]
        # Sheet 02's ``validation_summary``: what the last validation found, on
        # the run itself, so a list screen needs no second query. A copy of the
        # issue rows, never the source of truth for approval.
        run.validation_summary = [
            {
                "issue_code": issue.issue_code,
                "severity": issue.severity,
                "employee_id": issue.employee_id,
                "message": issue.message,
            }
            for issue in issues
        ]
        run.save(update_fields=["validation_summary", "updated_at"])
        return issues


def blocking_issues(run) -> list[PayrollValidationIssue]:
    """Unresolved BLOCKING issues — exactly what stops an approval.

    A RESOLVED blocking issue does not block, which is the point of being able
    to resolve one: an employer who has looked at a below-minimum wage and
    accepted it must be able to proceed, with their name and reason on the row.
    """
    with tenant_context_of(run):
        return list(
            run.validation_issues.filter(severity=BLOCKING, acknowledged_at__isnull=True).order_by(
                "issue_code", "employee_id"
            )
        )


@transaction.atomic
def resolve_issue(issue, *, resolved_by, reason: str) -> PayrollValidationIssue:
    """Record that a person looked at this and decided to proceed anyway.

    The reason is mandatory and the person is named, for the same reason
    self-approval of leave carries both (D-174): a decision nobody signed is one
    nobody can be asked about eighteen months later.
    """
    if not reason.strip():
        raise ValueError(
            "Resolving a validation issue needs a reason. Someone is overriding a check "
            "that exists because payroll goes wrong in exactly this way, and the reason "
            "is what an auditor — or the employee — will be shown."
        )
    from django.utils import timezone

    with tenant_context_of(issue):
        issue.acknowledged_at = timezone.now()
        issue.acknowledged_by_user = resolved_by
        issue.resolution_reason = reason
        issue.save(update_fields=["acknowledged_at", "acknowledged_by_user", "resolution_reason"])
    return issue
