"""The payroll run's lifecycle: draft → calculating → calculated → approved →
finalised, and the reversal that is the only correction.

**Transitions are enforced HERE, not by the CHECK on the column** (D-145, learned
on the import batch and restated as a test in chunk 6). The CHECK proves a status
is a value the enum knows; only ``transition()`` proves the move from the row's
previous value was legal. Every status change in this module goes through it, and
a status written any other way is a bug.

**Approval is the gate.** ``approve()`` re-validates first and refuses while any
unresolved BLOCKING issue stands, naming every one of them. It does not validate
once at the start and trust the result: the attendance could have changed, a
payslip could have been re-calculated, and an approval granted against a stale
picture is exactly the failure the gate exists to prevent.

**Finalisation is the point of no return, and it does four things together.** It
freezes each payslip's employee snapshot (invariant 7), marks the payslips
finalised so the trigger takes them out of reach (invariant 4), locks the
period's attendance days against further edits, closes the period, and rebuilds
the year-to-date cache from what was just finalised. All inside one transaction,
because a run that is half-finalised is a state nothing in this system knows how
to read.

**What this module does NOT do is calculate.** ``calculate()`` moves the run
through its states and calls an assembly that turns one employee into one
payslip — and that assembly is the next chunk. The lifecycle, the gate,
finalisation and reversal are all real and all tested; the arithmetic that fills
a payslip in is not here yet, which is why ``calculate()`` refuses rather than
producing empty payslips that would look like a successful run.
"""

from __future__ import annotations

import dataclasses

from django.db import models, transaction
from django.utils import timezone

from attendance.models import AttendanceDay
from calculators.base import ENGINE_VERSION
from core.managers import tenant_context_of
from payroll import validation, ytd
from payroll.models import PayPeriod, PayrollRun, Payslip

Status = PayrollRun.Status

#: The payslip figures a reversal carries negated. Everything numeric on the
#: header, so the reversal nets the original to zero column by column.
NEGATED_ON_REVERSAL = (
    "ordinary_hours",
    "overtime_hours",
    "days_worked",
    "gross_remuneration",
    "taxable_remuneration",
    "uif_remuneration",
    "sdl_remuneration",
    "paye",
    "uif_employee",
    "uif_employer",
    "sdl_employer",
    "total_earnings",
    "total_deductions",
    "total_employer_contributions",
    "net_pay",
)

#: Where a run may go from where it is. A state with no entry here is terminal.
#: Written as data rather than as a chain of ifs so that the whole machine can be
#: read, and asserted, in one place.
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    Status.DRAFT: {Status.CALCULATING},
    Status.CALCULATING: {Status.CALCULATED, Status.DRAFT, Status.FAILED},
    # A calculation that broke — not one that refused an employee, which is a
    # blocking issue on that employee (D-292) — may be tried again.
    Status.FAILED: {Status.CALCULATING},
    Status.CALCULATED: {Status.APPROVED, Status.CALCULATING},
    Status.APPROVED: {Status.FINALISED, Status.CALCULATED},
    Status.FINALISED: {Status.REVERSED},
    Status.REVERSED: set(),
}


class PayrollRunError(RuntimeError):
    """The run cannot go where it was asked to go."""


class ApprovalRefusedError(PayrollRunError):
    """Blocking validation issues stand. Named separately because a caller wants
    to catch this one and show the issues, not treat it as a programming error."""

    def __init__(self, message: str, issues):
        super().__init__(message)
        self.issues = issues


@dataclasses.dataclass(frozen=True)
class FinalisationReport:
    """What finalising actually did, for the screen that reports it."""

    payslips_finalised: int
    attendance_days_locked: int
    ytd_rows_rebuilt: int


def transition(run: PayrollRun, to_status: str, **fields) -> PayrollRun:
    """Move a run to a new status, or refuse and say why.

    The ONE place a run's status changes. ``fields`` are saved in the same write,
    so a status and the timestamp that evidences it are never two writes with a
    window between them.
    """
    allowed = LEGAL_TRANSITIONS.get(run.status, set())
    if to_status not in allowed:
        raise PayrollRunError(
            f"A run cannot go from {run.status} to {to_status}. From {run.status} it may "
            f"go to {', '.join(sorted(allowed)) or 'nowhere — that status is terminal'}. "
            f"The CHECK on the column proves a status is a known VALUE; this is what "
            f"proves the MOVE is legal."
        )

    with tenant_context_of(run):
        run.status = to_status
        for name, value in fields.items():
            setattr(run, name, value)
        run.save(update_fields=["status", *fields, "updated_at"])
    return run


@transaction.atomic
def open_run(
    period: PayPeriod, *, opened_by=None, run_type: str = PayrollRun.RunType.REGULAR
) -> PayrollRun:
    """Start a run over a period. A second run over the same period is a
    CORRECTION run and numbers itself accordingly."""
    with tenant_context_of(period):
        if period.status == PayPeriod.Status.CLOSED:
            raise PayrollRunError(
                f"Period {period.period_number} is closed. Reopening it is a deliberate, "
                f"counted act (pay_period.reopened_count) and not something starting a run "
                f"should do quietly."
            )
        last = period.runs.order_by("-run_number").first()
        if last is not None and last.status not in (Status.FINALISED, Status.REVERSED):
            raise PayrollRunError(
                f"Run {last.run_number} over this period is still {last.status}. Two live "
                f"runs over one period would each produce a payslip for the same employee "
                f"for the same days."
            )
        return PayrollRun.objects.create(
            tenant=period.tenant,
            employer=period.pay_group.employer,
            pay_period=period,
            run_number=(last.run_number + 1) if last else 1,
            run_type=run_type,
            status=Status.DRAFT,
            engine_version=ENGINE_VERSION,
        )


def payslip_number(run: PayrollRun, employee, *, suffix: str = "") -> str:
    """Sheet 02's ``payslip_number``, unique per tenant: the period's month,
    the pay group, the run and the employee. Readable on a printed page and at
    most 30 characters. A reversal appends ``-R`` to the number it reverses."""
    period = run.pay_period
    who = employee.employee_number or str(employee.pk)
    return f"{period.period_end:%Y%m}-{period.pay_group_id}-{run.run_number}-{who}{suffix}"[:30]


def calculate(run: PayrollRun) -> None:
    """Turn the run's employees into payslips. NOT BUILT — the next chunk.

    Refuses rather than moving the run to ``calculated`` with nothing in it. A
    run that reports itself calculated and holds no payslips is a run somebody
    approves, and the validation gate's ``no_payslips`` check would then be the
    only thing between that and a closed period with nobody paid. One guard deep
    is not deep enough for this.
    """
    raise NotImplementedError(
        "Assembling a payslip — reading each employee's attendance, leave, remuneration "
        "and tax profile, resolving the statutory rows for the period and calling the "
        "calculators — is the next chunk. The run's lifecycle, its validation gate, "
        "finalisation and reversal are built and tested; the arithmetic that fills a "
        "payslip in is not."
    )


def approve(run: PayrollRun, *, approved_by) -> PayrollRun:
    """Approve a calculated run, or refuse and name every blocking issue.

    Re-validates first. Trusting an earlier validation would approve against a
    picture that may have moved — the attendance re-imported, a payslip
    re-calculated — and the gate would have passed on facts that are no longer
    true.
    """
    validation.validate(run)
    blocking = validation.blocking_issues(run)
    if blocking:
        raise ApprovalRefusedError(
            f"{len(blocking)} blocking issue(s) stand on this run and it cannot be "
            f"approved:\n"
            + "\n".join(f"  - [{issue.issue_code}] {issue.message}" for issue in blocking)
            + "\nEach must be fixed, or resolved by a named person with a reason.",
            blocking,
        )
    return transition(
        run, Status.APPROVED, approved_at=timezone.now(), approved_by_user=approved_by
    )


@transaction.atomic
def finalise(run: PayrollRun, *, finalised_by) -> FinalisationReport:
    """Freeze the run: snapshots, payslips, attendance, the period, the cache.

    One transaction, because a half-finalised run is a state nothing in this
    system knows how to read — payslips frozen but the period still open, or the
    period closed with the year-to-date cache still describing last month.
    """
    when = timezone.now()
    period = run.pay_period

    with tenant_context_of(run):
        payslips = list(run.payslips.select_related("employee"))
        for payslip in payslips:
            payslip.employee_snapshot = snapshot_of(payslip.employee, on_date=period.period_end)
            payslip.is_finalised = True
            payslip.finalised_at = when
            payslip.save(
                update_fields=["employee_snapshot", "is_finalised", "finalised_at", "updated_at"]
            )

        # Invariant 4 for the attendance behind the pay: a day that has been paid
        # for is not edited afterwards. The column is still the forward-reference
        # BigIntegerField P5 created before payroll_run existed (O-29).
        locked = AttendanceDay.objects.filter(
            employee__in=[payslip.employee_id for payslip in payslips],
            work_date__gte=period.period_start,
            work_date__lte=period.period_end,
            locked_by_payroll_run_id_ref__isnull=True,
        ).update(status=AttendanceDay.Status.LOCKED, locked_by_payroll_run_id_ref=run.pk)

        transition(run, Status.FINALISED, finalised_at=when, finalised_by_user=finalised_by)

        period.status = PayPeriod.Status.CLOSED
        period.closed_at = when
        period.save(update_fields=["status", "closed_at", "updated_at"])

    rebuilt = 0
    for payslip in payslips:
        rebuilt += len(ytd.rebuild(payslip.employee, period.tax_year, as_at=when))

    return FinalisationReport(
        payslips_finalised=len(payslips),
        attendance_days_locked=locked,
        ytd_rows_rebuilt=rebuilt,
    )


def snapshot_of(employee, *, on_date=None) -> dict:
    """What a payslip freezes about a person (invariant 7).

    Read from the EFFECTIVE-DATED rows in force on the date, not from the
    employee's cache columns: ``current_pay_basis`` is a cache refreshed as at
    today (D-107), and today is not the date this payslip is for. The position,
    the rate and the bank account each live in their own dated table and each is
    read at the period's own date, so a reprint in 2029 shows what March 2026
    showed.

    Never the ID number or the account number themselves — both are encrypted
    (D-77) and the ``_last4`` columns are what a person recognises anyway.
    """
    from employees.models import EmployeeBankAccount, EmployeePosition, EmployeeRemuneration

    when = on_date or timezone.localdate()

    def in_force(model, start="effective_from", end="effective_to"):
        """The row in force on the date. ``employee_bank_account`` names its own
        columns ``active_from``/``active_to`` rather than the effective-dated
        pair every other table uses, so the names are parameters — reading the
        wrong ones raises here rather than silently snapshotting nothing."""
        return (
            model.objects.filter(employee=employee, **{f"{start}__lte": when})
            .filter(models.Q(**{f"{end}__isnull": True}) | models.Q(**{f"{end}__gte": when}))
            .order_by(f"-{start}")
            .first()
        )

    with tenant_context_of(employee):
        position = in_force(EmployeePosition)
        remuneration = in_force(EmployeeRemuneration)
        bank = in_force(EmployeeBankAccount, start="active_from", end="active_to")
        return {
            "as_at": when.isoformat(),
            "full_name": f"{employee.first_name} {employee.last_name}".strip(),
            "employee_number": employee.employee_number,
            "id_number_last4": employee.id_number_last4,
            "position": position.job_title if position else "",
            "pay_basis": remuneration.pay_basis if remuneration else "",
            "pay_rate": str(remuneration.rate_amount) if remuneration else "",
            "bank_reference": bank.account_number_last4 if bank else "",
        }


@transaction.atomic
def reverse(run: PayrollRun, *, reversed_by, reason: str) -> PayrollRun:
    """Reverse a finalised run: a NEW run, with a reversing payslip per payslip.

    Invariant 4. Nothing about the original moves — it cannot, the trigger sees
    to that — and the correction is a new run whose payslips are the negation of
    the originals. A replacement run comes after, as an ordinary run over the
    reopened period.
    """
    if run.status != Status.FINALISED:
        raise PayrollRunError(
            f"Only a finalised run is reversed; this one is {run.status}. An unfinalised "
            f"run is corrected by re-calculating it, which changes nothing anybody has "
            f"been given."
        )
    if not reason.strip():
        raise PayrollRunError(
            "Reversing a finalised payroll run needs a reason. Somebody has already been "
            "paid against it and been handed the document."
        )

    with tenant_context_of(run):
        reversal = PayrollRun.objects.create(
            tenant=run.tenant,
            employer=run.employer,
            pay_period=run.pay_period,
            run_number=run.run_number + 1,
            run_type=PayrollRun.RunType.CORRECTION,
            status=Status.DRAFT,
            engine_version=ENGINE_VERSION,
            reverses_run=run,
            notes=reason,
        )
        when = timezone.now()
        for original in run.payslips.select_related("employee"):
            mirror = Payslip.objects.create(
                tenant=run.tenant,
                payroll_run=reversal,
                employee=original.employee,
                pay_period=original.pay_period,
                engagement=original.engagement,
                payslip_number=f"{original.payslip_number[:28]}-R",
                employee_snapshot=original.employee_snapshot,
                pay_basis=original.pay_basis,
                rate_used=original.rate_used,
                payment_method=original.payment_method,
                bank_account=original.bank_account,
                is_termination_payslip=original.is_termination_payslip,
                is_reversal=True,
                reverses_payslip=original,
                # Every figure negated, so netting the two is how a correction
                # reaches the year-to-date and the IRP5.
                **{name: -getattr(original, name) for name in NEGATED_ON_REVERSAL},
            )
            for line in original.lines.all():
                mirror.lines.create(
                    tenant=run.tenant,
                    payroll_component=line.payroll_component,
                    component_type=line.component_type,
                    component_code=line.component_code,
                    sars_source_code=line.sars_source_code,
                    source_code=line.source_code,
                    description=f"Reversal: {line.description}"[:150],
                    line_order=line.line_order,
                    units=None if line.units is None else -line.units,
                    unit_type=line.unit_type,
                    rate=line.rate,
                    multiplier=line.multiplier,
                    amount_unrounded=-line.amount_unrounded,
                    amount=-line.amount,
                    is_taxable=line.is_taxable,
                    is_uif_base=line.is_uif_base,
                    is_sdl_base=line.is_sdl_base,
                    calculation_note=line.calculation_note,
                )
            mirror.is_finalised = True
            mirror.finalised_at = when
            mirror.save(update_fields=["is_finalised", "finalised_at", "updated_at"])

        # Collected INSIDE the pinned context. Reading reversal.payslips after
        # the block returns nothing — RLS with no tenant pinned answers "none",
        # not "not allowed" — and the rebuild below would silently do nothing,
        # leaving the year-to-date still showing the reversed run. A test caught
        # exactly that; the query reads like plain attribute access and is not.
        mirrored = list(reversal.payslips.select_related("employee"))

        transition(run, Status.REVERSED)
        transition(reversal, Status.CALCULATING)
        transition(reversal, Status.CALCULATED)
        transition(reversal, Status.APPROVED, approved_at=when, approved_by_user=reversed_by)
        transition(reversal, Status.FINALISED, finalised_at=when, finalised_by_user=reversed_by)

    for payslip in mirrored:
        ytd.rebuild(payslip.employee, run.pay_period.tax_year, as_at=when)

    return reversal
