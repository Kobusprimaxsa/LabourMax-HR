"""The termination payout: prepared from the rows, reviewed by a person, paid by a run.

The caller's half of ``calculators/termination.py`` (chunk 5, D-224 to D-227),
and sheet 02's ``termination_payout`` workflow (D-308):

* ``build_input()`` reads everything the calculator needs as at the termination
  date — the rate, the notice band, the leave ledger, the severance rule, the
  bonus cycle — and computes nothing itself.
* ``prepare()`` prices it and writes the DRAFT row, working included.
* ``review()`` is a person agreeing to the figure.
* ``for_payslip()`` is what the assembly calls: it refuses an unprepared or
  unreviewed payout, re-prices it from the rows, and refuses again if a part has
  moved since the review. A reviewed figure is what somebody agreed to.

**Declared, never inferred.** Whether notice was worked or paid in lieu is
``employee_engagement.notice_worked``, and a termination that leaves it blank
refuses: s38 is the employer's election, and paying four weeks nobody elected
to pay is as wrong as not paying them.

**D-185 is enforced by construction.** A negative leave balance is passed to the
calculator only as a figure to REPORT; the calculator never subtracts it, and
the validation gate raises it as a blocking issue for a person to decide
(``leave/negative_balances.py::at_termination()``). The payout is not reduced.

**What this build does not do, and says so:** it aggregates no earlier
engagement's service (s84(1), O-26); it recovers no outstanding loan balance
from the final payment (``outstanding_deductions`` is nil — see the model); it
computes no s39(2) accommodation offset (no column captures the agreed value);
and SEVERANCE, where due, is priced here but REFUSED on the payslip, because a
severance benefit is taxed on a SARS directive against the lump sum table and
no directive is captured anywhere yet (O-50).
"""

from __future__ import annotations

import calendar
import datetime
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Q

from attendance.models import AttendanceDay
from calculators.base import ZERO, StatutoryFigure
from calculators.termination import (
    NoticeBand,
    NoticeUnit,
    ProRataLeaveRule,
    SeveranceRule,
    TerminationInput,
    TerminationRefusedError,
    TerminationResult,
    termination_payout,
)
from core.managers import tenant_context_of
from employees.models import EmployeeEngagement, EmployeeRemuneration
from employees.probation import is_on_probation
from employees.remuneration import weekly_wage
from leave.balances import is_actually_stale, recompute_cycle
from leave.cycles import _sector_area_of
from leave.models import LeaveApplication, LeaveApplicationDay, LeaveCycle
from leave.negative_balances import at_termination
from payroll import bonus as bonuses
from payroll.models import TerminationPayout
from payroll.periods import working_days_between
from statutory import resolve
from statutory.models import StatutoryParameter

PRO_RATA_PARAMETER = "PRO_RATA_LEAVE_MIN_SERVICE_MONTHS"
ANNUAL = "ANNUAL"
ATTENDANCE_DRIVEN = {"hourly", "daily"}
CENTS = Decimal("0.01")

Status = TerminationPayout.Status


class PayoutRefusedError(Exception):  # noqa: N818 — reads as the fact it reports
    """The payout cannot be prepared, or cannot be paid as it stands. ``code``
    is stable, so the assembly can key a blocking issue on it (D-234)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- the input


def _service(start: datetime.date, last_day: datetime.date):
    """Completed years, completed months, and months as a decimal whose only job
    is to fall on the right side of s40(c)'s "longer than four months"."""
    span = relativedelta(last_day + datetime.timedelta(days=1), start)
    months = span.years * 12 + span.months
    days_in_month = calendar.monthrange(last_day.year, last_day.month)[1]
    fraction = Decimal(span.days) / Decimal(days_in_month)
    return span.years, months, Decimal(months) + fraction


def _annual_cycles(engagement):
    """The engagement's ANNUAL cycles, each RECOMPUTED if stale by either
    measure. ``balance_quantity`` is a cache (invariant 3): a posted transaction
    marks it stale and nothing refreshes it until somebody reads through
    ``leave/balances.py`` — reading the column raw paid a leaver nothing for
    leave the ledger plainly held, and a test caught it."""
    cycles = LeaveCycle.objects.filter(engagement=engagement, leave_type__code=ANNUAL).order_by(
        "cycle_start"
    )
    return [
        recompute_cycle(cycle) if cycle.is_stale or is_actually_stale(cycle) else cycle
        for cycle in cycles
    ]


def _days_worked(engagement, remuneration, start, end) -> Decimal:
    """s40(c)(i)'s "days on which the employee worked or was entitled to be paid"
    in the incomplete cycle. For an attendance-driven basis the captured days say
    it; for a salary, the working days of the pattern less the unpaid ones."""
    employee = engagement.employee
    days = AttendanceDay.objects.filter(employee=employee, work_date__gte=start, work_date__lte=end)
    if remuneration.pay_basis in ATTENDANCE_DRIVEN:
        return sum((row.days_worked_equivalent for row in days), ZERO)
    unpaid = days.filter(day_type=AttendanceDay.DayType.ABSENT_UNPAID).count()
    unpaid_leave = LeaveApplicationDay.objects.filter(
        leave_application__employee=employee,
        leave_application__status=LeaveApplication.Status.APPROVED,
        leave_date__gte=start,
        leave_date__lte=end,
        is_working_day=True,
        is_paid=False,
    )
    return (
        working_days_between(start, end, remuneration.days_per_week)
        - Decimal(unpaid)
        - sum((day.day_portion for day in unpaid_leave), ZERO)
    )


def build_input(engagement: EmployeeEngagement) -> TerminationInput:
    """Everything the calculator needs, read as at the termination date."""
    left = engagement.termination_date
    if left is None:
        raise PayoutRefusedError(
            "not_terminated", "This engagement has no termination date; nothing is owed yet."
        )
    if engagement.notice_worked is None:
        raise PayoutRefusedError(
            "notice_not_declared",
            "Whether notice was worked or paid in lieu is not recorded on the engagement. "
            "s38 payment in lieu is the employer's election; record notice_worked.",
        )
    employee = engagement.employee
    with tenant_context_of(engagement):
        remuneration = (
            EmployeeRemuneration.objects.filter(engagement=engagement, effective_from__lte=left)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=left))
            .order_by("-effective_from")
            .first()
        )
        if remuneration is None:
            raise PayoutRefusedError(
                "no_remuneration",
                f"No remuneration is in force on the last day of service, {left:%d %B %Y}.",
            )
        sector = employee.employer.sector
        area = _sector_area_of(employee, left)
        years, months, months_decimal = _service(engagement.start_date, left)

        notice_band = None
        if not engagement.notice_worked:
            band = resolve.notice_band(
                sector,
                left,
                employment_start_date=engagement.start_date,
                sector_area=area,
                on_probation=is_on_probation(engagement, left),
            )
            notice_band = NoticeBand(
                notice_value=band.notice_value,
                notice_unit=NoticeUnit(band.notice_unit),
                table=band._meta.db_table,
                row_id=band.pk,
            )

        # The leave ledger. A cycle that ended by the last day is complete
        # (s40(b)); the one the last day falls in is incomplete (s40(c)).
        due = {"days": ZERO, "hours": ZERO}
        incomplete = {"days": ZERO, "hours": ZERO}
        taken_in_incomplete = ZERO
        days_worked = ZERO
        negative = ZERO
        for cycle in _annual_cycles(engagement):
            balance = cycle.balance_quantity
            if balance < ZERO:
                negative += balance
            if cycle.cycle_start <= left < cycle.cycle_end:
                incomplete[cycle.unit] += balance
                if cycle.unit == LeaveCycle.Unit.DAYS:
                    taken_in_incomplete += -cycle.taken_quantity
                days_worked = _days_worked(engagement, remuneration, cycle.cycle_start, left)
            elif balance > ZERO:
                due[cycle.unit] += balance

        leave_rules = resolve.leave_rules(sector, left, area)
        termination_rules = resolve.termination_rules(sector, left, area)
        minimum = resolve.parameter(PRO_RATA_PARAMETER, left)

        return TerminationInput(
            calculated_for=left,
            weekly_rate=weekly_wage(remuneration),
            daily_rate=remuneration.derived_daily_rate,
            hourly_rate=remuneration.derived_hourly_rate,
            days_per_week=remuneration.days_per_week,
            hours_per_week=remuneration.hours_per_week,
            remuneration_is_variable=False,  # O-46
            notice_is_paid_in_lieu=not engagement.notice_worked,
            notice_band=notice_band,
            leave_due_days=due["days"],
            leave_due_hours=due["hours"],
            incomplete_cycle_days=incomplete["days"],
            incomplete_cycle_hours=incomplete["hours"],
            leave_taken_in_incomplete_cycle_days=taken_in_incomplete,
            days_worked_in_incomplete_cycle=days_worked,
            pro_rata_rule=ProRataLeaveRule(
                days_worked_per_leave_day=Decimal(leave_rules.annual_accrual_ratio_days_worked),
                table=leave_rules._meta.db_table,
                row_id=leave_rules.pk,
            ),
            pro_rata_minimum_service_months=StatutoryFigure(
                value=minimum.value_numeric,
                table=StatutoryParameter._meta.db_table,
                row_id=minimum.pk,
                description=minimum.source_reference,
            ),
            months_of_service=months_decimal,
            negative_leave_balance=negative,
            severance_rule=SeveranceRule(
                weeks_per_completed_year=termination_rules.severance_weeks_per_completed_year,
                requires_operational_reason=(
                    termination_rules.severance_requires_operational_reason
                ),
                table=termination_rules._meta.db_table,
                row_id=termination_rules.pk,
            ),
            dismissed_for_operational_requirements=engagement.triggers_severance,
            completed_years_of_service=Decimal(years),
            annual_bonus=bonuses.termination_bonus(employee, left),
        )


def price(engagement: EmployeeEngagement) -> tuple[TerminationInput, TerminationResult]:
    """Price the payout from the rows as they stand. Reads; never writes."""
    try:
        data = build_input(engagement)
        return data, termination_payout(data)
    except resolve.StatutoryValueMissingError as missing:
        raise PayoutRefusedError("reference_data_missing", str(missing)) from missing
    except TerminationRefusedError as refused:
        raise PayoutRefusedError("termination_refused", str(refused)) from refused


# ------------------------------------------------------------- the workflow


def _parts(result: TerminationResult) -> dict[str, Decimal]:
    """The four stored amounts, each the sum of its own ROUNDED lines — the
    figures the payslip will show, so the stored total is the payslip's total."""

    def rounded(*codes):
        return sum(
            (line.amount.rounded for line in result.lines if line.component_code in codes), ZERO
        )

    return {
        "notice_pay_amount": rounded("NOTICE_PAY"),
        "leave_payout_amount": rounded("LEAVE_PAYOUT"),
        "severance_amount": rounded("SEVERANCE"),
        "bonus_pro_rata_amount": rounded("BONUS_PRO_RATA"),
    }


def _detail(data: TerminationInput, result: TerminationResult, engagement) -> dict:
    trace = result.trace
    return {
        "trace": {
            "calculator": trace.calculator,
            "calculated_for": trace.calculated_for.isoformat(),
            "inputs": dict(trace.inputs),
            "statutory_rows": [list(pair) for pair in trace.statutory_rows],
            "outputs": dict(trace.outputs),
            "warnings": list(trace.warnings),
        },
        "lines": [
            {
                "component_code": line.component_code,
                "description": line.description,
                "units": str(line.units),
                "rate": str(line.rate),
                "amount": str(line.amount.rounded),
            }
            for line in result.lines
        ],
        "negative_leave_balances": [
            {
                "leave_type": item.leave_type_code,
                "cycle_start": item.cycle.cycle_start.isoformat(),
                "balance": str(item.balance),
                "unit": item.unit,
            }
            for item in at_termination(engagement)
        ],
    }


def _monthly_rate(engagement) -> Decimal:
    """The derived monthly rate in force on the last day — sheet 02's "basis for
    the pro-rata bonus", recorded for the certificate of service pack. The bonus
    itself is priced off the weekly wage by ``calculators/bonus.py``."""
    left = engagement.termination_date
    with tenant_context_of(engagement):
        row = (
            EmployeeRemuneration.objects.filter(engagement=engagement, effective_from__lte=left)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=left))
            .order_by("-effective_from")
            .first()
        )
    return row.derived_monthly_rate.quantize(Decimal("0.0001"), ROUND_HALF_UP)


def prepare(engagement: EmployeeEngagement) -> TerminationPayout:
    """Price the payout and write it as a DRAFT, working included. Re-preparing
    a draft or a reviewed payout replaces its figures and returns it to draft —
    a review covered the old figures, not these."""
    data, result = price(engagement)
    parts = _parts(result)
    weeks = ZERO
    if data.notice_band is not None:
        weeks = data.notice_band.notice_value
        if data.notice_band.notice_unit is NoticeUnit.DAYS:
            weeks = weeks / data.days_per_week
    bonus_months = 0
    if data.annual_bonus is not None and result.pro_rata_bonus.exact:
        bonus_months = next(
            int(line.units) for line in result.lines if line.component_code == "BONUS_PRO_RATA"
        )
    severance_weeks = next(
        (line.units for line in result.lines if line.component_code == "SEVERANCE"), ZERO
    )
    years, months, _ = _service(engagement.start_date, engagement.termination_date)
    values = {
        "tenant": engagement.tenant,
        "employee": engagement.employee,
        "termination_date": engagement.termination_date,
        "termination_reason_code": engagement.termination_reason_code,
        "completed_months_service": months,
        "completed_years_service": years,
        "weekly_wage_used": result.rates.weekly.quantize(Decimal("0.0001"), ROUND_HALF_UP),
        "daily_wage_used": result.rates.per_day().quantize(Decimal("0.0001"), ROUND_HALF_UP),
        "monthly_wage_used": _monthly_rate(engagement),
        "notice_weeks_required": weeks.quantize(CENTS, ROUND_HALF_UP),
        "notice_worked": not data.notice_is_paid_in_lieu,
        "accrued_leave_days": (
            data.leave_due_days + max(data.incomplete_cycle_days, ZERO)
        ).quantize(Decimal("0.001"), ROUND_HALF_UP),
        "severance_applicable": bool(parts["severance_amount"]),
        "severance_weeks": severance_weeks,
        "bonus_applicable": data.annual_bonus is not None,
        "bonus_months_worked": bonus_months,
        "outstanding_deductions": ZERO,
        "total_payout_gross": sum(parts.values(), ZERO),
        "severance_tax_treatment": (
            TerminationPayout.SeveranceTaxTreatment.DIRECTIVE_REQUIRED
            if parts["severance_amount"]
            else TerminationPayout.SeveranceTaxTreatment.STANDARD
        ),
        "status": Status.DRAFT,
        "calculation_detail": _detail(data, result, engagement),
        **parts,
    }
    with transaction.atomic(), tenant_context_of(engagement):
        existing = TerminationPayout.objects.filter(engagement=engagement).first()
        if existing is not None and existing.status not in (Status.DRAFT, Status.REVIEWED):
            raise PayoutRefusedError(
                "payout_already_processed",
                f"This payout is {existing.status}. A processed payout was paid by run "
                f"{existing.payroll_run_id}; correcting it is a reversal of that run.",
            )
        if existing is None:
            return TerminationPayout.objects.create(engagement=engagement, **values)
        for name, value in values.items():
            setattr(existing, name, value)
        existing.save()
        return existing


def review(payout: TerminationPayout, *, reviewed_by) -> TerminationPayout:
    """A person agrees to the figure. Only a draft is reviewed."""
    if payout.status != Status.DRAFT:
        raise PayoutRefusedError(
            "payout_not_draft",
            f"Only a draft payout is reviewed; this one is {payout.status}.",
        )
    with transaction.atomic(), tenant_context_of(payout):
        payout.status = Status.REVIEWED
        payout.calculation_detail = {
            **payout.calculation_detail,
            "review": {"by": reviewed_by.email, "by_user_id": reviewed_by.pk},
        }
        payout.save(update_fields=["status", "calculation_detail", "updated_at"])
    return payout


def for_payslip(engagement: EmployeeEngagement) -> tuple[TerminationPayout, TerminationResult]:
    """The reviewed payout for a leaver's final payslip, re-priced and checked."""
    with tenant_context_of(engagement):
        payout = TerminationPayout.objects.filter(engagement=engagement).first()
    if payout is None:
        raise PayoutRefusedError(
            "termination_payout_not_prepared",
            f"This employee's service ended on {engagement.termination_date:%d %B %Y} and no "
            f"termination payout has been prepared. Prepare it, check the working, and "
            f"review it before the final payslip is priced.",
        )
    if payout.status != Status.REVIEWED:
        raise PayoutRefusedError(
            "termination_payout_not_reviewed",
            f"The termination payout is {payout.status}. A person reviews the notice, leave, "
            f"severance and bonus figures before they are paid.",
        )
    _, result = price(engagement)
    now = _parts(result)
    moved = {
        name: (getattr(payout, name), figure)
        for name, figure in now.items()
        if getattr(payout, name) != figure
    }
    if moved:
        raise PayoutRefusedError(
            "termination_payout_changed",
            "The rows have moved since the payout was reviewed: "
            + "; ".join(f"{name} reviewed {was}, now {is_}" for name, (was, is_) in moved.items())
            + ". Prepare and review it again.",
        )
    if result.severance.exact:
        raise PayoutRefusedError(
            "severance_needs_directive",
            f"Severance of {result.severance} is due (BCEA s41). A severance benefit is taxed "
            f"on a SARS directive against the lump sum table (code 3901), and no directive is "
            f"captured in this build (O-50). The final payslip cannot be priced without one.",
        )
    return payout, result
