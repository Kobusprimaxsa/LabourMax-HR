"""Reproduce a payslip from its stored traces ALONE — P7's "Done when" (D-313).

Invariant 5: when an employee disputes a figure from eighteen months ago, the
answer is a stored record, not a re-run of today's code against today's rows.
This module is that answer made executable: for every trace on a payslip it
opens each reference row BY THE KEY THE TRACE RECORDED (never "the row in force
on the date", which could be a correction loaded since), rebuilds the
calculator's input from the trace's own inputs, runs the calculator, and
compares what comes out with what was stored.

It reads no attendance, no remuneration, no tax profile and no ledger — a
reversed run may have deleted the attendance, and an employee may have changed
banks and rates since. If a payslip only reproduces with those, invariant 5 is
not met, and ``reproduce()`` says which trace fails and how.

A trace written by a calculator this module cannot replay is reported, never
skipped: a replay that quietly passed over a calculator it did not know would be
exactly the silent guard this codebase keeps finding.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from calculators.attendance import AttendanceDayInput, AttendanceDayResult
from calculators.base import CalculationTrace, Money, StatutoryFigure
from calculators.bonus import BonusInput, RateBasis, WeeklyWage, annual_bonus
from calculators.gross import DayPay, GrossInput, PayBasis, gross_pay
from calculators.leave_pay import LeavePayInput, leave_pay
from calculators.net import Deduction, NetInput, net_pay
from calculators.paye import MedicalCredit, PayeInput, TaxBracket, TaxStatus, employees_tax
from calculators.recurring import (
    AccommodationCeiling,
    LineKind,
    RecurringInput,
    RecurringLine,
    recurring_lines,
)
from calculators.remuneration import AveragingWindow
from calculators.sdl import SdlInput, levy
from calculators.termination import (
    NoticeBand,
    NoticeUnit,
    ProRataLeaveRule,
    SeveranceRule,
    TerminationInput,
    termination_payout,
)
from calculators.uif import UifInput, contribution
from core.managers import tenant_context_of
from payroll.models import PayrollCalculationTrace
from statutory import models as statutory

AVERAGING_WEEKS = "VARIABLE_EARNINGS_AVERAGE_WEEKS"


class ReplayError(Exception):
    """A trace this module cannot reproduce — named, never skipped."""


def _d(text: str) -> Decimal | None:
    return None if text == "" else Decimal(text)


def _b(text: str) -> bool:
    return text == "True"


def _date(text: str) -> datetime.date | None:
    return None if text == "" else datetime.date.fromisoformat(text)


@dataclasses.dataclass
class _Rows:
    """The reference rows one trace recorded, opened by their keys."""

    keys: list[tuple[str, int]]

    def of(self, model) -> list:
        ids = [row_id for table, row_id in self.keys if table == model._meta.db_table]
        found = {row.pk: row for row in model.objects.filter(pk__in=ids)}
        missing = set(ids) - set(found)
        if missing:
            raise ReplayError(
                f"{model._meta.db_table} row(s) {sorted(missing)} recorded by the trace no "
                f"longer exist. Reference rows are never deleted; this one was."
            )
        return [found[row_id] for row_id in ids]

    def one(self, model):
        rows = self.of(model)
        return rows[0] if rows else None

    def parameter(self, code: str) -> StatutoryFigure | None:
        row = next(
            (row for row in self.of(statutory.StatutoryParameter) if row.parameter_code == code),
            None,
        )
        return None if row is None else _figure(row)


def _figure(row) -> StatutoryFigure:
    return StatutoryFigure(
        value=row.value_numeric,
        table=row._meta.db_table,
        row_id=row.pk,
        description=row.source_reference,
    )


# ------------------------------------------------------------------- replays


def _gross(inputs, rows: _Rows, when):
    from payroll.assembly import _premium_rates

    days = []
    for key in sorted(k for k in inputs if k.startswith("day_")):
        (on, day_type, ordinary_day, standby, scheduled, *buckets) = inputs[key].split("|")
        ordinary, overtime, sunday, holiday, night, guaranteed, standby_worked, dwe = map(
            Decimal, buckets
        )
        days.append(
            DayPay(
                day=AttendanceDayInput(
                    work_date=datetime.date.fromisoformat(on),
                    day_type=day_type,
                    time_in=None,
                    time_out=None,
                    unpaid_break_minutes=0,
                    is_standby=_b(standby),
                    is_ordinary_working_day=_b(ordinary_day),
                    scheduled_ordinary_hours=Decimal(scheduled),
                ),
                hours=AttendanceDayResult(
                    ordinary_hours=ordinary,
                    overtime_hours=overtime,
                    sunday_hours=sunday,
                    public_holiday_hours=holiday,
                    night_hours=night,
                    paid_hours_guaranteed=guaranteed,
                    standby_hours_worked=standby_worked,
                    days_worked_equivalent=dwe,
                ),
            )
        )
    return gross_pay(
        GrossInput(
            calculated_for=when,
            pay_basis=PayBasis(inputs["pay_basis"]),
            days=tuple(days),
            rates=_premium_rates(rows.one(statutory.WorkingTimeRuleSet)),
            hourly_rate=Decimal(inputs["hourly_rate"]),
            daily_rate=Decimal(inputs["daily_rate"]),
            ordinary_shift_hours=Decimal(inputs["ordinary_shift_hours"]),
            period_rate=Decimal(inputs["period_rate"]),
            working_days_in_period=Decimal(inputs["working_days_in_period"]),
            above_bcea_earnings_threshold=_b(inputs["above_bcea_earnings_threshold"]),
            further_unpaid_days=Decimal(inputs["further_unpaid_days"]),
        )
    ).trace


def _window(inputs, rows: _Rows) -> AveragingWindow | None:
    weeks = rows.parameter(AVERAGING_WEEKS)
    if weeks is None:
        return None
    return AveragingWindow(
        weeks=weeks,
        weeks_available=Decimal(inputs["window_weeks_available"]),
        remuneration=Decimal(inputs["window_remuneration"]),
    )


def _leave_pay(inputs, rows: _Rows, when):
    return leave_pay(
        LeavePayInput(
            calculated_for=when,
            leave_days=Decimal(inputs["leave_days"]),
            leave_hours=Decimal(inputs["leave_hours"]),
            daily_rate=Decimal(inputs["daily_rate"]),
            hourly_rate=Decimal(inputs["hourly_rate"]),
            remuneration_is_variable=_b(inputs["remuneration_is_variable"]),
            window=_window(inputs, rows),
            days_per_week=Decimal(inputs["days_per_week"]),
            hours_per_week=Decimal(inputs["hours_per_week"]),
        )
    ).trace


def _paye(inputs, rows: _Rows, when):
    credit = rows.one(statutory.MedicalTaxCreditRate)
    return employees_tax(
        PayeInput(
            calculated_for=when,
            remuneration=Decimal(inputs["remuneration"]),
            allowable_deductions=Decimal(inputs["allowable_deductions"]),
            annual_payment=Decimal(inputs["annual_payment"]),
            periods_in_year=Decimal(inputs["periods_in_year"]),
            periods_worked=Decimal(inputs["periods_worked"]),
            brackets=tuple(
                TaxBracket(
                    income_from=row.income_from,
                    income_to=row.income_to,
                    base_tax=row.base_tax,
                    marginal_rate_percent=row.marginal_rate_pct,
                    table=row._meta.db_table,
                    row_id=row.pk,
                )
                for row in sorted(rows.of(statutory.PayeTaxBracket), key=lambda r: r.income_from)
            ),
            rebates=tuple(
                StatutoryFigure(
                    value=row.annual_amount,
                    table=row._meta.db_table,
                    row_id=row.pk,
                    description=row.get_rebate_type_display(),
                )
                for row in rows.of(statutory.PayeRebate)
            ),
            medical_scheme_members=int(inputs["medical_scheme_members"]),
            medical_credit=(
                None
                if credit is None
                else MedicalCredit(
                    main_member_monthly=credit.main_member_monthly,
                    first_dependant_monthly=credit.first_dependant_monthly,
                    additional_dependant_monthly=credit.additional_dependant_monthly,
                    table=credit._meta.db_table,
                    row_id=credit.pk,
                )
            ),
            tax_status=TaxStatus(inputs["tax_status"]),
            directive_number=inputs["directive_number"],
            directive_percentage=_d(inputs["directive_percentage"]),
            directive_amount=_d(inputs["directive_amount"]),
            directive_valid_to=_date(inputs.get("directive_valid_to", "")),
        )
    ).trace


def _uif(inputs, rows: _Rows, when):
    return contribution(
        UifInput(
            calculated_for=when,
            remuneration=Decimal(inputs["remuneration"]),
            commission=Decimal(inputs["commission"]),
            excluded_remuneration=Decimal(inputs["excluded_remuneration"]),
            monthly_ceiling=rows.parameter("UIF_MONTHLY_CEILING"),
            employee_rate_percent=rows.parameter("UIF_EMPLOYEE_RATE_PCT"),
            employer_rate_percent=rows.parameter("UIF_EMPLOYER_RATE_PCT"),
            is_exempt=_b(inputs["is_exempt"]),
            exemption_reason=inputs["exemption_reason"],
        )
    ).trace


def _sdl(inputs, rows: _Rows, when):
    return levy(
        SdlInput(
            calculated_for=when,
            leviable_amount=Decimal(inputs["leviable_amount"]),
            rate_percent=rows.parameter("SDL_RATE_PCT"),
            employer_is_liable=_b(inputs["employer_is_liable"]),
            exemption_reason=inputs["exemption_reason"],
        )
    ).trace


def _recurring(inputs, rows: _Rows, when):
    rule_set = rows.one(statutory.WorkingTimeRuleSet)
    lines = []
    for key, text in inputs.items():
        if key == "basic":
            continue
        code, _, line_id = key.rpartition("_")
        fields = dict(part.split("=", 1) for part in text.split("; ")[1:])
        lines.append(
            RecurringLine(
                line_id=int(line_id),
                component_code=code,
                description=code,
                kind=LineKind(text.split("; ")[0]),
                amount=_d(fields["amount"].replace("None", "")),
                percentage_of_basic=_d(fields["percentage_of_basic"].replace("None", "")),
                cap_percent=_d(fields["cap_percent"].replace("None", "")),
                owed=_d(fields["owed"].replace("None", "")),
                is_accommodation=_b(fields["accommodation"]),
            )
        )
    return recurring_lines(
        RecurringInput(
            calculated_for=when,
            basic=Decimal(inputs["basic"]),
            lines=tuple(lines),
            accommodation_ceiling=(
                None
                if rule_set is None
                else AccommodationCeiling(
                    capped=rule_set.accommodation_deduction_capped,
                    max_percent=rule_set.accommodation_deduction_max_pct,
                    table=rule_set._meta.db_table,
                    row_id=rule_set.pk,
                )
            ),
        )
    ).trace


def _net(inputs, rows: _Rows, when):
    deductions = [
        (int(key.rpartition("_")[2]), key.rpartition("_")[0], Decimal(value))
        for key, value in inputs.items()
        if key != "earnings"
    ]
    return net_pay(
        NetInput(
            calculated_for=when,
            earnings=(Money.of(Decimal(inputs["earnings"])),),
            deductions=tuple(
                Deduction(code, Money.of(amount), is_statutory=code in ("PAYE", "UIF_EE"))
                for _, code, amount in sorted(deductions)
            ),
        )
    ).trace


def _bonus_input(inputs, rule) -> BonusInput:
    wages = []
    for key in sorted(k for k in inputs if k.startswith("wage_")):
        start, end, weekly = inputs[key].split("|")
        wages.append(WeeklyWage(_date(start), _date(end), Decimal(weekly)))
    return BonusInput(
        calculated_for=_date(inputs["as_at"]),
        rule=rule,
        cycle_start=_date(inputs["cycle_start"]),
        cycle_end=_date(inputs["cycle_end"]),
        service_start=_date(inputs["service_start"]),
        service_end=_date(inputs["service_end"]),
        as_at=_date(inputs["as_at"]),
        wages=tuple(wages),
        rate_basis=RateBasis(inputs["rate_basis"]),
        part_first_month_counts=_b(inputs["part_first_month_counts"]),
        is_termination=_b(inputs["is_termination"]),
        qualifies=_b(inputs["qualifies"]),
        disqualified_because=inputs["disqualified_because"],
    )


def _bonus(inputs, rows: _Rows, when):
    from payroll.bonus import bonus_rule

    return annual_bonus(
        _bonus_input(inputs, bonus_rule(rows.one(statutory.TerminationRuleSet)))
    ).trace


def _termination(inputs, rows: _Rows, when):
    from payroll.bonus import bonus_rule

    band = rows.one(statutory.TerminationNoticeBand)
    leave_rules = rows.one(statutory.LeaveRuleSet)
    termination_rules = rows.one(statutory.TerminationRuleSet)
    bonus_inputs = {
        k.removeprefix("bonus_"): v for k, v in inputs.items() if k.startswith("bonus_")
    }
    return termination_payout(
        TerminationInput(
            calculated_for=when,
            weekly_rate=Decimal(inputs["weekly_rate"]),
            daily_rate=Decimal(inputs["daily_rate"]),
            hourly_rate=Decimal(inputs["hourly_rate"]),
            days_per_week=Decimal(inputs["days_per_week"]),
            hours_per_week=Decimal(inputs["hours_per_week"]),
            remuneration_is_variable=_b(inputs["remuneration_is_variable"]),
            window=_window(inputs, rows),
            notice_is_paid_in_lieu=_b(inputs["notice_is_paid_in_lieu"]),
            notice_band=(
                None
                if band is None
                else NoticeBand(
                    notice_value=band.notice_value,
                    notice_unit=NoticeUnit(band.notice_unit),
                    table=band._meta.db_table,
                    row_id=band.pk,
                )
            ),
            accommodation_offset=Decimal(inputs["accommodation_offset"]),
            leave_due_days=Decimal(inputs["leave_due_days"]),
            leave_due_hours=Decimal(inputs["leave_due_hours"]),
            incomplete_cycle_days=Decimal(inputs["incomplete_cycle_days"]),
            incomplete_cycle_hours=Decimal(inputs["incomplete_cycle_hours"]),
            leave_taken_in_incomplete_cycle_days=Decimal(
                inputs["leave_taken_in_incomplete_cycle_days"]
            ),
            days_worked_in_incomplete_cycle=Decimal(inputs["days_worked_in_incomplete_cycle"]),
            pro_rata_rule=(
                None
                if leave_rules is None
                else ProRataLeaveRule(
                    days_worked_per_leave_day=Decimal(leave_rules.annual_accrual_ratio_days_worked),
                    table=leave_rules._meta.db_table,
                    row_id=leave_rules.pk,
                )
            ),
            pro_rata_minimum_service_months=rows.parameter("PRO_RATA_LEAVE_MIN_SERVICE_MONTHS"),
            months_of_service=Decimal(inputs["months_of_service"]),
            negative_leave_balance=Decimal(inputs["negative_leave_balance"]),
            severance_rule=(
                None
                if termination_rules is None
                else SeveranceRule(
                    weeks_per_completed_year=termination_rules.severance_weeks_per_completed_year,
                    requires_operational_reason=(
                        termination_rules.severance_requires_operational_reason
                    ),
                    table=termination_rules._meta.db_table,
                    row_id=termination_rules.pk,
                )
            ),
            dismissed_for_operational_requirements=_b(
                inputs["dismissed_for_operational_requirements"]
            ),
            unreasonably_refused_alternative_employment=_b(
                inputs["unreasonably_refused_alternative_employment"]
            ),
            completed_years_of_service=Decimal(inputs["completed_years_of_service"]),
            annual_bonus=(
                _bonus_input(bonus_inputs, bonus_rule(termination_rules))
                if "as_at" in bonus_inputs
                else None
            ),
        )
    ).trace


REPLAYS = {
    "gross.gross_pay": _gross,
    "leave_pay.leave_pay": _leave_pay,
    "paye.employees_tax": _paye,
    "uif.contribution": _uif,
    "sdl.levy": _sdl,
    "recurring.recurring_lines": _recurring,
    "net.net_pay": _net,
    "bonus.annual_bonus": _bonus,
    "termination.termination_payout": _termination,
}


@dataclasses.dataclass(frozen=True)
class Mismatch:
    calculator: str
    sequence: int
    what: str


def replay(row: PayrollCalculationTrace) -> CalculationTrace:
    """One stored trace, re-run from its own inputs and the rows it recorded."""
    replayer = REPLAYS.get(row.calculator_name)
    if replayer is None:
        raise ReplayError(f"No replay for {row.calculator_name}; a trace is never skipped.")
    rows = _Rows([(table, int(row_id)) for table, row_id in row.reference_rows_used])
    return replayer(row.inputs, rows, row.calculated_for)


def reproduce(payslip) -> list[Mismatch]:
    """Every trace on the payslip, replayed. An empty list is invariant 5 met."""
    with tenant_context_of(payslip):
        rows = list(PayrollCalculationTrace.objects.filter(payslip=payslip).order_by("sequence"))
        return compare(rows)


def compare(rows: list[PayrollCalculationTrace]) -> list[Mismatch]:
    """Replay each stored trace and say where it disagrees with itself. Separate
    from ``reproduce()`` because a stored trace cannot be edited (the table is
    append-only by trigger) — so the tests that prove a disagreement is caught
    hand this altered copies."""
    if not rows:
        return [Mismatch("-", 0, "the payslip has no traces at all")]
    mismatches = []
    for row in rows:
        try:
            replayed = replay(row)
        except ReplayError as failed:
            mismatches.append(Mismatch(row.calculator_name, row.sequence, str(failed)))
            continue
        if dict(replayed.outputs) != row.outputs:
            mismatches.append(
                Mismatch(
                    row.calculator_name,
                    row.sequence,
                    f"outputs {dict(replayed.outputs)} != stored {row.outputs}",
                )
            )
        recorded = [[table, int(row_id)] for table, row_id in row.reference_rows_used]
        if [list(pair) for pair in replayed.statutory_rows] != recorded:
            mismatches.append(
                Mismatch(
                    row.calculator_name,
                    row.sequence,
                    f"rows {list(replayed.statutory_rows)} != stored {recorded}",
                )
            )
    return mismatches
