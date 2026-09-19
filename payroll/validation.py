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

So on this database today, with 0 of 21 versions verified, a run refuses. That
is not this module failing to work — it is this module working, and there is a
test that pins exactly that.

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
from django.db.models import Sum

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

    Two separate failures, and they are not the same thing. There may be no
    usable version AT ALL for the period — nothing verified, or nothing applying
    that far back — or there may be one whose author said, in
    ``data_current_through``, how far they were willing to vouch for it.
    """
    period = run.pay_period
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

    if version.data_current_through and period.period_end > version.data_current_through:
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
        lines = payslip.lines.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        stated = payslip.gross_earnings - payslip.total_deductions
        if lines != stated:
            found.append(
                Finding(
                    "totals_do_not_match_lines",
                    BLOCKING,
                    f"This payslip states {stated} (gross {payslip.gross_earnings} less "
                    f"deductions {payslip.total_deductions}) and its lines add up to "
                    f"{lines}. A payslip whose total is not its own lines is one nobody "
                    f"can explain to the employee holding it.",
                    employee=payslip.employee,
                )
            )
    return found


def check_attendance_is_complete(run) -> list[Finding]:
    """P5's own hook, wired up at last.

    ``attendance/completeness.py`` was written in P5 with its direction settled
    deliberately: it answers "what is missing" ONLY for an attendance-driven pay
    basis, because for a salaried employee no row means an ordinary day worked
    and flagging every uncaptured day would be noise. P5's notes say "P7's
    validation gate will call this; it is not built here." This is that call.
    """
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


#: Every check, in the order an employer would want to read them: the ones about
#: the run and its data first, then the ones about individual people.
CHECKS = (
    check_reference_data,
    check_the_run_has_payslips,
    check_payslip_totals_match_their_lines,
    check_attendance_is_complete,
    check_overdrawn_leave,
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

        run.validation_issues.filter(resolved_at__isnull=True).delete()

        # A resolution is against a PROBLEM, not against a row. The problem is
        # identified by (code, employee) on this run — so a finding somebody has
        # already looked at and accepted is not re-raised, which is the whole
        # point of being able to resolve one. Keying on the row id instead would
        # make every resolution last exactly until the next validation.
        already_resolved = set(
            run.validation_issues.filter(resolved_at__isnull=False).values_list(
                "code", "employee_id"
            )
        )
        return [
            PayrollValidationIssue.objects.create(
                tenant=run.tenant,
                payroll_run=run,
                employee=finding.employee,
                code=finding.code,
                severity=finding.severity,
                message=finding.message,
            )
            for finding in found
            if (finding.code, finding.employee.pk if finding.employee else None)
            not in already_resolved
        ]


def blocking_issues(run) -> list[PayrollValidationIssue]:
    """Unresolved BLOCKING issues — exactly what stops an approval.

    A RESOLVED blocking issue does not block, which is the point of being able
    to resolve one: an employer who has looked at a below-minimum wage and
    accepted it must be able to proceed, with their name and reason on the row.
    """
    with tenant_context_of(run):
        return list(
            run.validation_issues.filter(severity=BLOCKING, resolved_at__isnull=True).order_by(
                "code", "employee_id"
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
        issue.resolved_at = timezone.now()
        issue.resolved_by_user = resolved_by
        issue.resolution_reason = reason
        issue.save(update_fields=["resolved_at", "resolved_by_user", "resolution_reason"])
    return issue
