"""Assembling one employee's payslip for one run — P7 chunk 8b.

The caller's half of every calculator at once: read the effective-dated rows in
force for the period, hand each pure function its inputs, and turn what comes
back into payslip lines, a header and traces. **Nothing here computes money.**
Every figure is a calculator's; this module only decides which rows it is
computed FROM, and what to call the result.

``build()`` reads and never writes, so the same function prices a payslip in
``payroll/runs.py::calculate()`` and explains, in ``payroll/validation.py``, why
an employee has none. A refusal raises ``CannotPrice`` with a stable code and a
message the employer can act on; ``calculate()`` skips that employee and the
validation gate reports the refusal as a blocking issue on them (D-292), while
everybody else is paid.

**The rows are read as at the period, never today** (D-107's lesson again): the
remuneration, the tax profile, the schedule and the rule set in force at the
period's end — or at the engagement's last day, for a leaver.

**Four things refuse rather than guess** (D-219, D-291):

* a standby day and above-threshold overtime — ``calculators/gross.py`` already
  refuses both, naming O-19/O-20/O-22 and O-24;
* hours worked on a day that is both a Sunday and a public holiday (O-40);
* a salaried pay basis on a pay group of another frequency — a monthly rate on
  a weekly run has no conversion this build has read;
* UIF on a weekly or fortnightly run where the period's remuneration exceeds a
  pro-rated monthly ceiling. UICA s6(2) states the ceiling per MONTH and no
  weekly figure is loaded; below that line the ceiling cannot bite and the
  monthly figure is handed in unchanged (O-47).

**Two declared facts have no column yet** (O-46): BCEA s35(4)'s "remuneration
is variable" and s6(3)'s "above the earnings threshold". Both are passed as
FALSE — the contractual rate for leave pay, and every premium paid — until the
employer can declare them.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from django.db.models import Q

from attendance import scheduling
from attendance.models import AttendanceDay
from calculators.attendance import AttendanceDayInput, AttendanceDayResult
from calculators.base import ZERO, CalculationTrace, Money, StatutoryFigure
from calculators.gross import (
    DayPay,
    GrossInput,
    GrossPayRefusedError,
    NightAllowanceKind,
    PayBasis,
    PremiumRates,
    gross_pay,
)
from calculators.leave_pay import LeavePayInput, leave_pay
from calculators.paye import (
    MedicalCredit,
    PayeInput,
    PayeInputError,
    TaxBracket,
    TaxStatus,
    employees_tax,
)
from calculators.recurring import (
    AccommodationCeiling,
    LineKind,
    RecurringInput,
    RecurringLine,
    RecurringRefusedError,
    recurring_lines,
)
from calculators.remuneration import RemunerationRefusedError
from calculators.sdl import SdlInput, levy
from calculators.uif import UifInput, contribution
from core.managers import tenant_context_of
from employees.models import (
    Employee,
    EmployeeBankAccount,
    EmployeeEngagement,
    EmployeeRecurringComponent,
    EmployeeRemuneration,
    EmployeeTaxProfile,
)
from employers.models import EmployerStatutoryRegistration, PayGroup, PayrollComponent
from leave.cycles import _sector_area_of
from leave.models import LeaveApplication, LeaveApplicationDay
from payroll.models import PayrollRun, PayslipLine
from payroll.periods import _cadence, working_days_between
from statutory import resolve
from statutory.models import (
    MedicalTaxCreditRate,
    PayeRebate,
    PayeTaxBracket,
    SarsSourceCode,
    StatutoryParameter,
    WorkingTimeRuleSet,
)

#: Pay periods in a tax year, by the cadence the pay group repeats on. The
#: calendar, like twelve months in a year — never a figure a gazette carries.
PERIODS_IN_YEAR = {
    PayGroup.PayFrequency.MONTHLY: Decimal("12"),
    PayGroup.PayFrequency.FORTNIGHTLY: Decimal("26"),
    PayGroup.PayFrequency.WEEKLY: Decimal("52"),
}
SALARIED = {PayBasis.WEEKLY, PayBasis.FORTNIGHTLY, PayBasis.MONTHLY}

UIF_CEILING = "UIF_MONTHLY_CEILING"
UIF_EMPLOYEE_RATE = "UIF_EMPLOYEE_RATE_PCT"
UIF_EMPLOYER_RATE = "UIF_EMPLOYER_RATE_PCT"
SDL_RATE = "SDL_RATE_PCT"


class CannotPrice(Exception):  # noqa: N818 — reads as the fact it reports
    """This employee cannot be paid on this run until something is fixed.

    ``code`` is stable — it keys the blocking issue (D-234 keys a resolution on
    code and employee) — and the message says what to fix.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass
class DraftLine:
    component: PayrollComponent
    description: str
    amount: Money
    units: Decimal | None = None
    unit_type: str = ""
    rate: Decimal | None = None
    multiplier: Decimal | None = None
    note: str = ""
    recurring_component: EmployeeRecurringComponent | None = None


@dataclasses.dataclass
class Draft:
    """One payslip, priced and not yet written."""

    employee: Employee
    engagement: EmployeeEngagement
    remuneration: EmployeeRemuneration
    bank_account: EmployeeBankAccount | None
    lines: list[DraftLine]
    traces: list[CalculationTrace]
    header: dict


# ----------------------------------------------------------------- who is paid


def _in_force(queryset, day, start="effective_from", end="effective_to"):
    return (
        queryset.filter(**{f"{start}__lte": day})
        .filter(Q(**{f"{end}__isnull": True}) | Q(**{f"{end}__gt": day}))
        .order_by(f"-{start}")
        .first()
    )


def employees_in(run: PayrollRun) -> list[Employee]:
    """Everybody the run owes a payslip: engaged at some point in the period,
    with a remuneration row on the run's pay group in force during it."""
    period = run.pay_period
    with tenant_context_of(run):
        ids = (
            EmployeeRemuneration.objects.filter(
                pay_group=period.pay_group, effective_from__lte=period.period_end
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=period.period_start))
            .filter(engagement__start_date__lte=period.period_end)
            .filter(
                Q(engagement__termination_date__isnull=True)
                | Q(engagement__termination_date__gte=period.period_start)
            )
            .values_list("employee_id", flat=True)
            .distinct()
        )
        return list(Employee.objects.filter(pk__in=list(ids)).order_by("last_name", "pk"))


# ------------------------------------------------------------------- the rows


def _component(code: str) -> PayrollComponent:
    component = PayrollComponent.objects.shared().filter(code=code, is_active=True).first()
    if component is None:
        raise CannotPrice(
            "component_missing",
            f"The payroll component {code} is not in the shared catalogue. Run "
            f"`python manage.py seedcomponents`.",
        )
    return component


def _figure(code: str, on_date: datetime.date) -> StatutoryFigure:
    row = resolve.parameter(code, on_date)
    return StatutoryFigure(
        value=row.value_numeric,
        table=StatutoryParameter._meta.db_table,
        row_id=row.pk,
        description=row.source_reference,
    )


def _premium_rates(row: WorkingTimeRuleSet) -> PremiumRates:
    return PremiumRates(
        overtime_multiplier=row.overtime_multiplier,
        sunday_multiplier_ordinary=row.sunday_multiplier_ordinary,
        sunday_multiplier_non_ordinary=row.sunday_multiplier_non_ordinary,
        public_holiday_worked_multiplier=row.public_holiday_worked_multiplier,
        public_holiday_not_worked_paid=row.public_holiday_not_worked_paid,
        night_allowance_type=NightAllowanceKind(row.night_allowance_type),
        night_allowance_value=row.night_allowance_value,
        table=WorkingTimeRuleSet._meta.db_table,
        row_id=row.pk,
    )


def _day_pay(employee, row: AttendanceDay) -> DayPay:
    schedule = scheduling.current_schedule(employee, row.work_date)
    schedule_day = scheduling.schedule_day_for(schedule, row.work_date)
    return DayPay(
        day=AttendanceDayInput(
            work_date=row.work_date,
            day_type=row.day_type,
            time_in=row.time_in,
            time_out=row.time_out,
            unpaid_break_minutes=row.unpaid_break_minutes,
            is_standby=row.is_standby,
            is_ordinary_working_day=schedule_day.is_working_day if schedule_day else False,
            scheduled_ordinary_hours=schedule_day.ordinary_hours if schedule_day else ZERO,
        ),
        # The buckets as STORED at capture (invariant 2): never recomputed here.
        hours=AttendanceDayResult(
            ordinary_hours=row.ordinary_hours,
            overtime_hours=row.overtime_hours,
            sunday_hours=row.sunday_hours,
            public_holiday_hours=row.public_holiday_hours,
            night_hours=row.night_hours,
            paid_hours_guaranteed=row.paid_hours_guaranteed,
            standby_hours_worked=row.standby_hours_worked,
            days_worked_equivalent=row.days_worked_equivalent,
        ),
    )


def _refuse_an_unread_sunday_holiday(days: list[DayPay]) -> None:
    """D-291: hours worked on a date that is both a Sunday and a public holiday
    are priced by s16 or by s18, and which is O-40's question."""
    for pay in days:
        worked = (
            pay.hours.ordinary_hours
            + pay.hours.overtime_hours
            + pay.hours.sunday_hours
            + pay.hours.public_holiday_hours
        )
        date = pay.day.work_date
        if worked and date.weekday() == 6 and resolve.is_public_holiday(date):
            raise CannotPrice(
                "sunday_public_holiday",
                f"{worked} hour(s) were worked on {date:%A %d %B %Y}, which is both a Sunday "
                f"and a public holiday. BCEA s16 and s18 price that day differently and which "
                f"governs has not been read (O-40, D-291). Resolve this issue once the reading "
                f"is settled or the day is recaptured.",
            )


def _leave_days(employee, start, end, *, paid: bool):
    return LeaveApplicationDay.objects.filter(
        leave_application__employee=employee,
        leave_application__status=LeaveApplication.Status.APPROVED,
        leave_date__gte=start,
        leave_date__lte=end,
        is_working_day=True,
        is_paid=paid,
    )


def _further_unpaid_days(employee, period, engagement, remuneration, start, end) -> Decimal:
    """D-293: the unpaid days a salary is pro-rated for that are not
    ``absent_unpaid`` attendance rows."""
    unpaid = ZERO
    if engagement.start_date > period.period_start:
        unpaid += working_days_between(
            period.period_start,
            engagement.start_date - datetime.timedelta(days=1),
            remuneration.days_per_week,
        )
    left = engagement.termination_date
    if left is not None and left < period.period_end:
        unpaid += working_days_between(
            left + datetime.timedelta(days=1), period.period_end, remuneration.days_per_week
        )
    unpaid_leave = _leave_days(employee, start, end, paid=False)
    if unpaid_leave.filter(hours__isnull=False).exists():
        raise CannotPrice(
            "unpaid_leave_in_hours_on_a_salary",
            "Unpaid leave in this period is held in HOURS, and a salary is pro-rated in "
            "working days. Nothing in this system converts one into the other (D-164).",
        )
    unpaid += sum((day.day_portion for day in unpaid_leave), ZERO)
    return unpaid


# ------------------------------------------------------------- recurring lines

ACCOMMODATION = "ACCOM_DED"
PRICED_METHODS = {
    PayrollComponent.CalculationMethod.FIXED,
    PayrollComponent.CalculationMethod.PERCENTAGE_OF_BASE,
}


def owed_on(line: EmployeeRecurringComponent) -> Decimal | None:
    """What is still owed on a loan line: the principal as captured, less every
    FINALISED payslip line that priced it (D-303). Derived, never stored —
    ``balance_outstanding`` is the principal and is never written after capture.
    A reversing payslip's line is negative, so a reversal puts the money back."""
    if line.balance_outstanding is None:
        return None
    recovered = sum(
        (
            row.amount
            for row in PayslipLine.objects.filter(
                recurring_component=line, payslip__is_finalised=True
            )
        ),
        ZERO,
    )
    return line.balance_outstanding - recovered


def _recurring_rows(employee, day) -> list[EmployeeRecurringComponent]:
    """The active recurring lines in force on the period's last day, each
    checked for what the calculator cannot see: consent, and whether this
    build can price the line at all."""
    rows = list(
        EmployeeRecurringComponent.objects.filter(
            employee=employee, is_active=True, effective_from__lte=day
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=day))
        .select_related("payroll_component__sars_source_code", "written_consent_file")
        .order_by("payroll_component__display_order", "pk")
    )
    for row in rows:
        component = row.payroll_component
        kind = component.component_type
        if kind not in (component.ComponentType.EARNING, component.ComponentType.DEDUCTION):
            raise CannotPrice(
                "recurring_type_not_built",
                f"A recurring {component.code} line is an {kind} component. Only recurring "
                f"earnings and deductions are priced; deactivate the line or remove it.",
            )
        if component.calculation_method not in PRICED_METHODS:
            raise CannotPrice(
                "recurring_method_not_built",
                f"A recurring {component.code} line uses the {component.calculation_method} "
                f"method. Recurring lines are priced as a fixed amount or a percentage of "
                f"basic, and nothing else is built.",
            )
        source = component.sars_source_code
        if kind == component.ComponentType.DEDUCTION:
            consent = row.written_consent_file
            if consent is None or consent.deleted_at is not None:
                raise CannotPrice(
                    "deduction_without_consent",
                    f"The recurring {component.code} deduction has no written consent on "
                    f"file. BCEA s34(1) permits a deduction without it only where a law, "
                    f"collective agreement, court order or arbitration award requires it.",
                )
            if source is not None:
                raise CannotPrice(
                    "deduction_affects_tax",
                    f"{component.code} is reported under SARS code {source.code}, which "
                    f"changes the employee's tax (a pension, provident or medical scheme "
                    f"contribution). That treatment is not built, and the line cannot be "
                    f"deducted as though it were not there.",
                )
        elif source is not None and source.code_group == SarsSourceCode.Group.FRINGE_BENEFIT:
            raise CannotPrice(
                "fringe_benefit_not_built",
                f"{component.code} is a fringe benefit (SARS code {source.code}): taxable but "
                f"not paid in cash. Fringe benefits are not built, and as a recurring "
                f"earning it would be paid out as money.",
            )
    return rows


def _price_recurring(rows, basic: Decimal, rules: WorkingTimeRuleSet, end: datetime.date):
    try:
        return recurring_lines(
            RecurringInput(
                calculated_for=end,
                basic=basic,
                lines=tuple(
                    RecurringLine(
                        line_id=row.pk,
                        component_code=row.payroll_component.code,
                        description=row.payroll_component.name,
                        kind=LineKind(row.payroll_component.component_type),
                        amount=row.amount,
                        percentage_of_basic=row.percentage_of_basic,
                        cap_percent=row.total_deduction_cap_pct,
                        owed=owed_on(row),
                        is_accommodation=(
                            row.payroll_component.is_system
                            and row.payroll_component.code == ACCOMMODATION
                        ),
                    )
                    for row in rows
                ),
                accommodation_ceiling=AccommodationCeiling(
                    capped=rules.accommodation_deduction_capped,
                    max_percent=rules.accommodation_deduction_max_pct,
                    table=WorkingTimeRuleSet._meta.db_table,
                    row_id=rules.pk,
                ),
            )
        )
    except RecurringRefusedError as refused:
        raise CannotPrice("recurring_refused", str(refused)) from refused


# ---------------------------------------------------------------------- build


def build(run: PayrollRun, employee: Employee) -> Draft:
    """Price one employee's payslip on one run. Reads; never writes.

    A statutory row that is not loaded for this employee's sector, area or
    dates is a refusal about THIS employee — another employee on the same run
    may resolve to a different instrument — so it is a ``CannotPrice`` too.
    """
    try:
        return _build(run, employee)
    except resolve.StatutoryValueMissingError as missing:
        raise CannotPrice("reference_data_missing", str(missing)) from missing


def _build(run: PayrollRun, employee: Employee) -> Draft:
    period = run.pay_period
    with tenant_context_of(employee):
        engagement = (
            EmployeeEngagement.objects.filter(employee=employee, start_date__lte=period.period_end)
            .filter(Q(termination_date__isnull=True) | Q(termination_date__gte=period.period_start))
            .order_by("-start_date")
            .first()
        )
        if engagement is None:
            raise CannotPrice("not_engaged", "No engagement covers this period.")
        start = max(period.period_start, engagement.start_date)
        end = min(period.period_end, engagement.termination_date or period.period_end)

        remuneration = _in_force(
            EmployeeRemuneration.objects.filter(employee=employee, pay_group=period.pay_group),
            end,
        )
        if remuneration is None:
            raise CannotPrice(
                "no_remuneration",
                f"No remuneration on this pay group is in force on {end:%d %B %Y}.",
            )
        basis = PayBasis(remuneration.pay_basis)
        cadence = _cadence(period.pay_group)
        if basis in SALARIED and basis.value != cadence:
            raise CannotPrice(
                "pay_basis_frequency",
                f"A {basis.value} rate on a {cadence} pay group. The period's pay would need a "
                f"conversion between the two that this build has not read; move the employee "
                f"to a {basis.value} pay group or capture a {cadence} rate.",
            )

        profile = _in_force(EmployeeTaxProfile.objects.filter(employee=employee), end)
        if profile is None:
            raise CannotPrice(
                "no_tax_profile",
                f"No tax profile is in force on {end:%d %B %Y}, so PAYE and UIF cannot be "
                f"worked out. Capture one on the employee's tax screen.",
            )

        sector = employee.employer.sector
        area = _sector_area_of(employee, end)
        rules = resolve.working_time_rules(sector, end, sector_area=area)
        if resolve.working_time_rules(sector, start, sector_area=area).pk != rules.pk:
            raise CannotPrice(
                "rule_set_changed_mid_period",
                f"The working time rules changed between {start:%d %B} and {end:%d %B %Y}. "
                f"Pricing one period under two instruments is not built.",
            )

        days = [
            _day_pay(employee, row)
            for row in AttendanceDay.objects.filter(
                employee=employee, work_date__gte=start, work_date__lte=end
            ).order_by("work_date")
        ]
        _refuse_an_unread_sunday_holiday(days)

        traces: list[CalculationTrace] = []
        lines: list[DraftLine] = []

        # ------------------------------------------------------------ gross
        try:
            gross = gross_pay(
                GrossInput(
                    calculated_for=end,
                    pay_basis=basis,
                    days=days,
                    rates=_premium_rates(rules),
                    hourly_rate=remuneration.derived_hourly_rate,
                    daily_rate=remuneration.derived_daily_rate,
                    ordinary_shift_hours=remuneration.hours_per_day,
                    period_rate=remuneration.rate_amount if basis in SALARIED else ZERO,
                    working_days_in_period=period.working_days_in_period,
                    above_bcea_earnings_threshold=False,  # O-46
                    further_unpaid_days=(
                        _further_unpaid_days(employee, period, engagement, remuneration, start, end)
                        if basis in SALARIED
                        else ZERO
                    ),
                )
            )
        except GrossPayRefusedError as refused:
            raise CannotPrice("gross_refused", str(refused)) from refused
        traces.append(gross.trace)
        for line in gross.lines:
            if line.amount.exact:
                lines.append(
                    DraftLine(
                        component=_component(line.component_code),
                        description=line.description,
                        amount=line.amount,
                        units=line.units,
                        unit_type=_unit_type(basis, line.component_code),
                        rate=line.rate,
                    )
                )

        # -------------------------------------------------------- leave pay
        if basis not in SALARIED:
            paid_leave = list(_leave_days(employee, start, end, paid=True))
            by_unit = {
                "days": sum((d.day_portion for d in paid_leave if d.hours is None), ZERO),
                "hours": sum((d.hours for d in paid_leave if d.hours is not None), ZERO),
            }
            for unit, quantity in by_unit.items():
                if not quantity:
                    continue
                try:
                    priced = leave_pay(
                        LeavePayInput(
                            calculated_for=end,
                            leave_days=quantity if unit == "days" else ZERO,
                            leave_hours=quantity if unit == "hours" else ZERO,
                            daily_rate=remuneration.derived_daily_rate,
                            hourly_rate=remuneration.derived_hourly_rate,
                            remuneration_is_variable=False,  # O-46
                            days_per_week=remuneration.days_per_week,
                            hours_per_week=remuneration.hours_per_week,
                        )
                    )
                except RemunerationRefusedError as refused:
                    raise CannotPrice("leave_pay_refused", str(refused)) from refused
                traces.append(priced.trace)
                lines.append(
                    DraftLine(
                        component=_component("LEAVE_PAY"),
                        description=priced.line.description,
                        amount=priced.amount,
                        units=quantity,
                        unit_type=unit,
                        rate=priced.rate_used.exact,
                    )
                )

        # ------------------------------------------------- recurring lines
        # Earnings join BEFORE the tax, UIF and SDL bases are summed, each on
        # its component's own flags; deductions come off after the statutory
        # ones (D-301).
        recurring_rows = _recurring_rows(employee, end)
        by_id = {row.pk: row for row in recurring_rows}
        recurring = None
        if recurring_rows:
            basic = sum(
                (line.amount.exact for line in lines if line.component.code == "BASIC"), ZERO
            )
            recurring = _price_recurring(recurring_rows, basic, rules, end)
            traces.append(recurring.trace)
            lines.extend(_recurring_line(item, by_id) for item in recurring.earnings)

        earnings = list(lines)

        def base(flag: str) -> Decimal:
            return sum(
                (line.amount.rounded for line in earnings if getattr(line.component, flag)),
                ZERO,
            )

        total_earnings = sum((line.amount.rounded for line in earnings), ZERO)
        taxable = base("is_taxable")
        uif_base = base("is_uif_base")
        sdl_base = base("is_sdl_base")

        # ------------------------------------------------------------ PAYE
        year = resolve.tax_year(period.payment_date)
        periods_in_year = PERIODS_IN_YEAR[cadence]
        in_period = (period.period_end - period.period_start).days + 1
        employed = (end - start).days + 1
        periods_worked = Decimal("1") if employed == in_period else Decimal(employed) / in_period
        age = _age_on(employee.date_of_birth, year.end_date)
        credit_row = resolve.medical_tax_credit(year)
        try:
            paye = employees_tax(
                PayeInput(
                    calculated_for=end,
                    remuneration=taxable,
                    allowable_deductions=ZERO,
                    annual_payment=ZERO,
                    periods_in_year=periods_in_year,
                    periods_worked=periods_worked,
                    brackets=tuple(
                        TaxBracket(
                            income_from=row.income_from,
                            income_to=row.income_to,
                            base_tax=row.base_tax,
                            marginal_rate_percent=row.marginal_rate_pct,
                            table=PayeTaxBracket._meta.db_table,
                            row_id=row.pk,
                        )
                        for row in resolve.paye_brackets(year)
                    ),
                    rebates=tuple(
                        StatutoryFigure(
                            value=row.annual_amount,
                            table=PayeRebate._meta.db_table,
                            row_id=row.pk,
                            description=row.get_rebate_type_display(),
                        )
                        for row in resolve.paye_rebates(year, age)
                    ),
                    medical_scheme_members=profile.medical_scheme_members or 0,
                    medical_credit=(
                        None
                        if credit_row is None
                        else MedicalCredit(
                            main_member_monthly=credit_row.main_member_monthly,
                            first_dependant_monthly=credit_row.first_dependant_monthly,
                            additional_dependant_monthly=credit_row.additional_dependant_monthly,
                            table=MedicalTaxCreditRate._meta.db_table,
                            row_id=credit_row.pk,
                        )
                    ),
                    tax_status=TaxStatus(profile.tax_status),
                    directive_number=profile.directive_number or "",
                    directive_percentage=profile.directive_percentage,
                    directive_amount=profile.directive_amount,
                    directive_valid_to=profile.directive_valid_to,
                )
            )
        except PayeInputError as refused:
            raise CannotPrice("paye_refused", str(refused)) from refused
        traces.append(paye.trace)

        # ------------------------------------------------------------- UIF
        ceiling = _figure(UIF_CEILING, end)
        periods_per_month = periods_in_year / Decimal("12")
        if cadence != PayGroup.PayFrequency.MONTHLY and uif_base > (
            ceiling.value / periods_per_month
        ):
            raise CannotPrice(
                "uif_ceiling_not_monthly",
                f"UIF remuneration of {uif_base} on a {cadence} run is above the monthly "
                f"ceiling spread over the period, and UICA s6(2) states the ceiling per month "
                f"only. How it applies to a {cadence} period has not been read (O-47).",
            )
        uif = contribution(
            UifInput(
                calculated_for=end,
                remuneration=uif_base,
                commission=ZERO,
                excluded_remuneration=ZERO,
                monthly_ceiling=ceiling,
                employee_rate_percent=_figure(UIF_EMPLOYEE_RATE, end),
                employer_rate_percent=_figure(UIF_EMPLOYER_RATE, end),
                is_exempt=profile.is_uif_exempt,
                exemption_reason=profile.uif_exempt_reason or "",
            )
        )
        traces.append(uif.trace)

        # ------------------------------------------------------------- SDL
        registration = _in_force(
            EmployerStatutoryRegistration.objects.filter(
                employer=employee.employer,
                registration_type=EmployerStatutoryRegistration.RegistrationType.SDL,
            ),
            end,
            start="registered_from",
            end="registered_to",
        )
        if registration is None:
            liable, reason = False, "No SDL registration is captured for this employer."
        elif registration.is_exempt:
            liable, reason = False, registration.exemption_reason or "Employer SDL-exempt."
        elif profile.is_sdl_exempt:
            liable, reason = False, "This employee's tax profile is SDL-exempt."
        else:
            liable, reason = True, ""
        sdl = levy(
            SdlInput(
                calculated_for=end,
                leviable_amount=sdl_base,
                rate_percent=_figure(SDL_RATE, end),
                employer_is_liable=liable,
                exemption_reason=reason,
            )
        )
        traces.append(sdl.trace)

        for code, description, amount in (
            ("PAYE", "PAYE", paye.tax),
            ("UIF_EE", "UIF (employee)", uif.employee),
            ("UIF_ER", "UIF (employer)", uif.employer),
            ("SDL_ER", "Skills Development Levy", sdl.levy),
        ):
            if amount.exact:
                lines.append(
                    DraftLine(component=_component(code), description=description, amount=amount)
                )

        other = [_recurring_line(item, by_id) for item in recurring.deductions] if recurring else []
        lines.extend(other)

        deductions = (
            paye.tax.rounded
            + uif.employee.rounded
            + sum((line.amount.rounded for line in other), ZERO)
        )
        contributions = uif.employer.rounded + sdl.levy.rounded
        net = total_earnings - deductions
        if net < ZERO:
            named = ", ".join(f"{line.component.code} {line.amount}" for line in other)
            raise CannotPrice(
                "negative_net",
                f"Deductions of {deductions} exceed earnings of {total_earnings}"
                + (f" (including {named})" if named else "")
                + ". A negative net is never stored (sheet 03); reduce or suspend a "
                "deduction for this period.",
            )

        bank = _in_force(
            EmployeeBankAccount.objects.filter(employee=employee),
            end,
            start="active_from",
            end="active_to",
        )

        return Draft(
            employee=employee,
            engagement=engagement,
            remuneration=remuneration,
            bank_account=bank,
            lines=lines,
            traces=traces,
            header={
                "pay_basis": basis.value,
                "rate_used": remuneration.rate_amount,
                "ordinary_hours": sum((p.hours.ordinary_hours for p in days), ZERO),
                "overtime_hours": sum((p.hours.overtime_hours for p in days), ZERO),
                "days_worked": sum((p.hours.days_worked_equivalent for p in days), ZERO),
                "gross_remuneration": total_earnings,
                "taxable_remuneration": taxable,
                "uif_remuneration": uif.contribution_base.rounded,
                "sdl_remuneration": sdl.leviable_amount.rounded,
                "paye": paye.tax.rounded,
                "uif_employee": uif.employee.rounded,
                "uif_employer": uif.employer.rounded,
                "sdl_employer": sdl.levy.rounded,
                "total_earnings": total_earnings,
                "total_deductions": deductions,
                "total_employer_contributions": contributions,
                "net_pay": net,
                "payment_method": (
                    bank.payment_method
                    if bank is not None and bank.payment_method in ("eft", "cash")
                    else "eft"
                ),
            },
        )


def _recurring_line(item, by_id) -> DraftLine:
    row = by_id[item.line_id]
    return DraftLine(
        component=row.payroll_component,
        description=item.description,
        amount=item.amount,
        rate=item.percentage,
        note=item.note,
        recurring_component=row,
    )


def _age_on(born: datetime.date, day: datetime.date) -> int:
    """Age on a date — for the rebates, on the LAST day of the tax year (G01 §4)."""
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def _unit_type(basis: PayBasis, component_code: str) -> str:
    """What a gross line's units count. A salary's BASIC is one period, a daily
    rate's is days, everything else the gross calculator emits is hours —
    except a fixed night allowance, which it counts in shifts."""
    if component_code == "BASIC":
        if basis in SALARIED:
            return PayslipLine.UnitType.NONE
        return PayslipLine.UnitType.DAYS if basis is PayBasis.DAILY else PayslipLine.UnitType.HOURS
    return PayslipLine.UnitType.HOURS
