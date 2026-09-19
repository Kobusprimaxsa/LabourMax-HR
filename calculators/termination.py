"""Termination payout — BCEA ss 38, 39, 40, 41, all calculated under s35.

Pure. Every quantity arrives already decided — the notice band from
``statutory.resolve``, the leave balances from P6's ledger, the completed years
from the engagement — and this module only prices them, at the one s35 rate
``calculators/remuneration.py`` works out (s35(5) names s21, s38 and s41
together, so they share it by the Act's own instruction).

**s38(1) — pay instead of notice.** "Instead of giving an employee notice in
terms of section 37, an employer may pay the employee the remuneration the
employee would have received, calculated in accordance with section 35, if the
employee had worked during the notice period." So it is the band's own period at
the s35 rate: a four-week band is four weekly wages, a one-working-day band is
one daily wage. The band carries its own unit (D-68) and this module reads it
rather than assuming weeks.

**SD1 clause 23(1)(d) does not conflict with that, and the note that said it
might was wrong about what the clause says.** The determination's own words:
"an employee or employer may terminate the contract without notice by paying
... in lieu of such notice **not less than** in the case of — (i) one working
day's notice, the daily wage the employee is receiving at the time of such
termination; (ii) four weeks' notice, **double the weekly wage** the employee is
receiving at the time of such termination." So SD1's figures are a FLOOR ("not
less than"), they attach to the same two bands the determination actually has
(one working day, four weeks), and for the four-week band the floor is two
weekly wages where s38 gives four. s38 is the higher figure in every case and
paying it satisfies both. There is no two-week band and never was — see D-224.

**s39(2) — accommodation.** "If an employee elects to remain in accommodation
... after the employer has terminated the employee's contract of employment in
terms of section 38, the remuneration that the employer is required to pay in
terms of section 38 is reduced by that portion of the remuneration that
represents the agreed value of the accommodation for the period that the
employee remains in the accommodation." A reduction of the notice payment only,
never of leave or severance, and never below zero.

**s40(b) — leave due and not taken**, priced by s21(1), which is
``calculators/leave_pay.py``. Called rather than reimplemented.

**s40(c) — the incomplete cycle**, and it is a FLOOR rather than a formula.
"if the employee has been in employment longer than four months ... (i) one
day's remuneration in respect of every 17 days on which the employee worked or
was entitled to be paid; or (ii) remuneration calculated on any basis that is at
least as favourable to the employee as that calculated in terms of subparagraph
(i)." P6's accrual engine already runs the employee's own basis into the ledger,
so what is owed is the GREATER of that balance and the 17-day ratio — and where
the ratio wins, the result says so, because an employer whose rule set accrues
less generously than s40(c)(i) is underpaying every leaver.

"**Longer than** four months" puts the boundary in the lower band: exactly four
months does not qualify. Read off the words, the same way each notice band's own
inclusivity was (D-158).

**s41(2) — severance.** "An employer must pay an employee who is dismissed for
reasons based on the employer's operational requirements severance pay equal to
at least one week's remuneration for each completed year of continuous service
with that employer, calculated in accordance with section 35." Three conditions,
all declared rather than derived: the dismissal must be for operational
requirements (s41(1)'s "economic, technological, structural or similar needs"),
the employee must not have unreasonably refused alternative employment
(s41(4)), and the years must be COMPLETED — a partial year counts for nothing.

**s84(1) is the caller's job and this build does not do it yet** (O-26). "For
the purposes of determining the length of an employee's employment with an
employer for any provision of this Act, previous employment with the same
employer must be taken into account if the break between the periods of
employment is less than one year." ``EmployeeEngagement.service_days_to()`` is
explicitly this-engagement-only, and nothing aggregates across a re-hire, so a
returning employee's severance and notice band are both understated. This module
takes the years as an input and says so here rather than silently accepting a
figure computed the wrong way.

**A negative leave balance is NEVER netted off** (D-185). Recovering one is a
BCEA s34 deduction and needs the employee's written consent, so it is surfaced
as a warning with its figure for a human to decide on — never subtracted.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from decimal import Decimal

from calculators.base import (
    ZERO,
    CalculationTrace,
    Money,
    PayslipLine,
    StatutoryFigure,
    as_text,
    rows_of,
)
from calculators.leave_pay import LeavePayInput, leave_pay
from calculators.remuneration import (
    AveragingWindow,
    RemunerationRefusedError,
    Section35Rates,
    section_35_rates,
)

CALCULATOR = "termination.termination_payout"


class TerminationRefusedError(RemunerationRefusedError):
    """The payout cannot be computed, and guessing would be worse."""


class NoticeUnit(enum.StrEnum):
    """Mirrors ``TerminationNoticeBand.NoticeUnit``, value for value. A band
    states its own unit because SD1's shortest is one WORKING DAY and every BCEA
    band is weeks (D-68).

    Two members, so pricing needs no fallback branch. If the model ever gains a
    third — months, say, which would need s35(3)'s four-and-one-third factor —
    ``payroll/tests/test_termination_boundary.py`` fails and names it, which is
    a better guard than an unreachable ``else`` nobody can test.
    """

    DAYS = "days"
    WEEKS = "weeks"


@dataclasses.dataclass(frozen=True)
class NoticeBand:
    """The band in force for this employee's service length, as resolved."""

    notice_value: Decimal
    notice_unit: NoticeUnit
    table: str
    row_id: int


@dataclasses.dataclass(frozen=True)
class SeveranceRule:
    """``termination_rule_set``'s severance columns, as in force."""

    weeks_per_completed_year: Decimal
    requires_operational_reason: bool
    table: str
    row_id: int


@dataclasses.dataclass(frozen=True)
class ProRataLeaveRule:
    """s40(c)(i)'s ratio, off ``leave_rule_set`` — the same
    ``annual_accrual_ratio_days_worked`` s20(2)(b) accrues by."""

    days_worked_per_leave_day: Decimal
    table: str
    row_id: int


@dataclasses.dataclass(frozen=True)
class TerminationInput:
    calculated_for: datetime.date

    #: The employee's own contractual rates (D-106). Weekly is the hub.
    weekly_rate: Decimal
    daily_rate: Decimal
    hourly_rate: Decimal
    days_per_week: Decimal
    hours_per_week: Decimal

    #: s35(4), declared (D-220).
    remuneration_is_variable: bool = False
    window: AveragingWindow | None = None

    # --- s38 -----------------------------------------------------------------
    #: False when the employee works out the notice period: it is then ordinary
    #: remuneration on the ordinary payslip, not a termination payment.
    notice_is_paid_in_lieu: bool = False
    notice_band: NoticeBand | None = None
    #: s39(2). The agreed value of accommodation the employee stays in, for the
    #: period they stay. Reduces the s38 payment and nothing else.
    accommodation_offset: Decimal = ZERO

    # --- s40(b) and s40(c) ---------------------------------------------------
    #: Leave due from COMPLETED cycles and not taken, in the unit the ledger
    #: holds it in (D-164). Exactly one of the two, or both zero.
    leave_due_days: Decimal = ZERO
    leave_due_hours: Decimal = ZERO
    #: The ledger's own accrual for the INCOMPLETE cycle, same units.
    incomplete_cycle_days: Decimal = ZERO
    incomplete_cycle_hours: Decimal = ZERO
    #: s40(c)(i)'s denominator input: days on which the employee "worked or was
    #: entitled to be paid" during the incomplete cycle.
    days_worked_in_incomplete_cycle: Decimal = ZERO
    pro_rata_rule: ProRataLeaveRule | None = None
    #: s40(c)'s "longer than four months", as loaded reference data.
    pro_rata_minimum_service_months: StatutoryFigure | None = None
    months_of_service: Decimal = ZERO
    #: D-185. Surfaced, never subtracted.
    negative_leave_balance: Decimal = ZERO

    # --- s41 -----------------------------------------------------------------
    severance_rule: SeveranceRule | None = None
    #: s41(1)-(2). Declared: whether a dismissal is for operational requirements
    #: is a fact about the dismissal, and it is already on the engagement row's
    #: own ``triggers_severance``.
    dismissed_for_operational_requirements: bool = False
    #: s41(4). Declared, and "unreasonably" is a judgement no calculator makes.
    unreasonably_refused_alternative_employment: bool = False
    #: s84(1) is the CALLER's job (O-26): previous employment with the same
    #: employer counts where the break was under a year, and nothing in this
    #: build aggregates it yet.
    completed_years_of_service: Decimal = ZERO


@dataclasses.dataclass(frozen=True)
class TerminationResult:
    lines: tuple[PayslipLine, ...]
    total: Money
    notice_pay: Money
    leave_due_pay: Money
    pro_rata_leave_pay: Money
    severance: Money
    rates: Section35Rates
    trace: CalculationTrace


def _notice_line(
    data: TerminationInput, rates: Section35Rates
) -> tuple[PayslipLine | None, list[str]]:
    """s38(1), reduced by s39(2)."""
    warnings: list[str] = []
    if not data.notice_is_paid_in_lieu:
        return None, warnings
    if data.notice_band is None:
        raise TerminationRefusedError(
            "Notice is being paid in lieu and no notice band was supplied. s38(1) pays "
            "'the remuneration the employee would have received ... if the employee had "
            "worked during the notice period', and the period comes from "
            "termination_notice_band — there is nothing here to assume."
        )

    band = data.notice_band
    rate = rates.per_day() if band.notice_unit is NoticeUnit.DAYS else rates.weekly

    gross = band.notice_value * rate
    if rate <= ZERO:
        raise TerminationRefusedError(
            f"The notice period is {band.notice_value} {band.notice_unit.value} and the "
            f"rate for it is {rate}. s38(1) pays what the employee would have earned by "
            f"working it, and there is no rate to pay it at."
        )

    amount = gross - data.accommodation_offset
    if data.accommodation_offset:
        if amount < ZERO:
            warnings.append(
                f"The s39(2) accommodation offset of {data.accommodation_offset} exceeds the "
                f"notice payment of {gross}; the payment is treated as nil rather than "
                f"negative. s39(2) reduces what is owed, it does not create a debt."
            )
            amount = ZERO
        else:
            warnings.append(
                f"Notice pay reduced by {data.accommodation_offset} under s39(2) for "
                f"accommodation the employee remains in."
            )

    return (
        PayslipLine(
            component_code="NOTICE_PAY",
            description="Payment instead of notice",
            units=band.notice_value,
            rate=rate,
            amount=Money.of(amount),
        ),
        warnings,
    )


def _leave_pay_for(
    data: TerminationInput, *, days: Decimal, hours: Decimal, description: str
) -> Money:
    """s21(1), through the calculator that already implements it."""
    if not days and not hours:
        return Money.of(ZERO)
    result = leave_pay(
        LeavePayInput(
            calculated_for=data.calculated_for,
            leave_days=days,
            leave_hours=hours,
            daily_rate=data.daily_rate,
            hourly_rate=data.hourly_rate,
            remuneration_is_variable=data.remuneration_is_variable,
            window=data.window,
            days_per_week=data.days_per_week,
            hours_per_week=data.hours_per_week,
        )
    )
    return result.amount


def _pro_rata_quantity(data: TerminationInput) -> tuple[Decimal, Decimal, list[str]]:
    """s40(c): the greater of the ledger's own accrual and the 17-day ratio.

    Returns (days, hours, warnings). The ratio is expressed in DAYS by the Act,
    so it can only be compared against a days-denominated cycle; an hours cycle
    is left to the ledger and the mismatch is said out loud.
    """
    warnings: list[str] = []
    if not data.incomplete_cycle_days and not data.incomplete_cycle_hours:
        if data.days_worked_in_incomplete_cycle:
            warnings.append(
                f"{data.days_worked_in_incomplete_cycle} day(s) worked in the incomplete "
                f"cycle and the ledger accrued nothing for it. s40(c) is still owed if the "
                f"employee was employed longer than the qualifying period."
            )

    if data.pro_rata_minimum_service_months is None:
        raise TerminationRefusedError(
            "s40(c)'s qualifying period was not supplied. It is "
            "PRO_RATA_LEAVE_MIN_SERVICE_MONTHS in statutory_parameter, and nothing here "
            "knows how many months four is."
        )
    # "longer than four months" — exactly four does not qualify, so the
    # boundary sits with the lower band (D-158's lesson, read off the words).
    if data.months_of_service <= data.pro_rata_minimum_service_months.value:
        return ZERO, ZERO, warnings

    if data.incomplete_cycle_hours:
        # An hours-denominated cycle cannot be compared against a ratio the Act
        # states in days without converting, and D-164 forbids converting.
        warnings.append(
            f"The incomplete cycle is held in hours ({data.incomplete_cycle_hours}), and "
            f"s40(c)(i)'s floor is stated in DAYS — one day for every 17 worked. The two "
            f"cannot be compared without a conversion this system does not do (D-164), so "
            f"the ledger's own accrual is paid and the statutory floor is not checked."
        )
        return ZERO, data.incomplete_cycle_hours, warnings

    if data.pro_rata_rule is None:
        raise TerminationRefusedError(
            "s40(c)(i)'s ratio was not supplied. It is "
            "leave_rule_set.annual_accrual_ratio_days_worked — the same ratio s20(2)(b) "
            "accrues by — and without it the statutory floor cannot be checked."
        )
    if data.pro_rata_rule.days_worked_per_leave_day <= ZERO:
        raise TerminationRefusedError(
            f"s40(c)(i)'s ratio is {data.pro_rata_rule.days_worked_per_leave_day} days "
            f"worked per leave day. Nothing can be divided by that."
        )

    statutory_floor = (
        data.days_worked_in_incomplete_cycle / data.pro_rata_rule.days_worked_per_leave_day
    )
    if statutory_floor > data.incomplete_cycle_days:
        warnings.append(
            f"s40(c)(i)'s floor of {Money.of(statutory_floor).exact} day(s) — one for every "
            f"{data.pro_rata_rule.days_worked_per_leave_day} of "
            f"{data.days_worked_in_incomplete_cycle} worked — exceeds the "
            f"{data.incomplete_cycle_days} day(s) the ledger accrued, so the floor is paid. "
            f"A rule set that accrues less generously than s40(c)(i) underpays every leaver."
        )
        return statutory_floor, ZERO, warnings
    return data.incomplete_cycle_days, ZERO, warnings


def _severance_line(
    data: TerminationInput, rates: Section35Rates
) -> tuple[PayslipLine | None, list[str]]:
    """s41(2), subject to s41(1) and s41(4)."""
    warnings: list[str] = []
    if not data.dismissed_for_operational_requirements:
        return None, warnings
    if data.severance_rule is None:
        raise TerminationRefusedError(
            "This is an operational-requirements dismissal and no severance rule was "
            "supplied. s41(2)'s 'at least one week's remuneration for each completed year' "
            "is termination_rule_set.severance_weeks_per_completed_year, which the Minister "
            "may vary under s41(3) — it is never a literal."
        )
    if data.unreasonably_refused_alternative_employment:
        # s41(4). Recorded rather than silently producing a zero line, because
        # "no severance" and "no severance BECAUSE of s41(4)" are different
        # facts and only one of them is defensible eighteen months later.
        warnings.append(
            "No severance paid: the employee unreasonably refused an offer of alternative "
            "employment (s41(4)). That refusal is a judgement the employer made and must be "
            "able to justify."
        )
        return None, warnings

    years = data.completed_years_of_service
    if years <= ZERO:
        warnings.append(
            "No severance paid: no COMPLETED year of continuous service (s41(2)). Note "
            "s84(1) — previous employment with the same employer counts where the break was "
            "under a year, and this build does not aggregate it (O-26)."
        )
        return None, warnings

    weeks = years * data.severance_rule.weeks_per_completed_year
    return (
        PayslipLine(
            component_code="SEVERANCE",
            description="Severance pay",
            units=weeks,
            rate=rates.weekly,
            amount=Money.of(weeks * rates.weekly),
        ),
        warnings,
    )


def termination_payout(data: TerminationInput) -> TerminationResult:
    """Everything owed to one employee on termination."""
    if bool(data.leave_due_days) and bool(data.leave_due_hours):
        raise TerminationRefusedError(
            f"Leave due is held in exactly one unit: {data.leave_due_days} day(s) and "
            f"{data.leave_due_hours} hour(s) were both given. Nothing in this system "
            f"converts between them (D-164)."
        )

    rates = section_35_rates(
        contractual_weekly=data.weekly_rate,
        contractual_daily=data.daily_rate,
        contractual_hourly=data.hourly_rate,
        remuneration_is_variable=data.remuneration_is_variable,
        window=data.window,
        days_per_week=data.days_per_week,
        hours_per_week=data.hours_per_week,
    )
    warnings: list[str] = list(rates.warnings)
    lines: list[PayslipLine] = []

    notice, notice_warnings = _notice_line(data, rates)
    warnings.extend(notice_warnings)
    if notice is not None:
        lines.append(notice)

    leave_due = _leave_pay_for(
        data,
        days=data.leave_due_days,
        hours=data.leave_due_hours,
        description="Leave due on termination",
    )
    if leave_due.exact:
        lines.append(
            PayslipLine(
                component_code="LEAVE_PAY",
                description="Annual leave due and not taken (s40(b))",
                units=data.leave_due_days or data.leave_due_hours,
                rate=rates.per_day() if data.leave_due_days else rates.per_hour(),
                amount=leave_due,
            )
        )

    pro_rata_days, pro_rata_hours, pro_rata_warnings = _pro_rata_quantity(data)
    warnings.extend(pro_rata_warnings)
    pro_rata = _leave_pay_for(
        data, days=pro_rata_days, hours=pro_rata_hours, description="Pro-rata leave"
    )
    if pro_rata.exact:
        lines.append(
            PayslipLine(
                component_code="LEAVE_PAY",
                description="Pro-rata leave for the incomplete cycle (s40(c))",
                units=pro_rata_days or pro_rata_hours,
                rate=rates.per_day() if pro_rata_days else rates.per_hour(),
                amount=pro_rata,
            )
        )

    severance, severance_warnings = _severance_line(data, rates)
    warnings.extend(severance_warnings)
    if severance is not None:
        lines.append(severance)

    if data.negative_leave_balance:
        # D-185, and it is the reason that decision exists: netting this off is
        # a BCEA s34 deduction and needs the employee's written consent.
        warnings.append(
            f"This employee's leave balance is overdrawn by {data.negative_leave_balance}. "
            f"It is NOT netted off this payout: recovering it is a s34 deduction requiring "
            f"the employee's written consent, and that is a decision for a person to make."
        )

    total = Money.of(sum((line.amount.exact for line in lines), ZERO))

    provenance = [
        source
        for source in (
            data.notice_band if data.notice_is_paid_in_lieu else None,
            data.window,
            data.pro_rata_rule if pro_rata_days else None,
            data.pro_rata_minimum_service_months,
            data.severance_rule if severance is not None else None,
        )
        if source is not None
    ]

    trace = CalculationTrace(
        calculator=CALCULATOR,
        calculated_for=data.calculated_for,
        inputs=as_text(
            weekly_rate=data.weekly_rate,
            daily_rate=data.daily_rate,
            hourly_rate=data.hourly_rate,
            remuneration_is_variable=data.remuneration_is_variable,
            notice_is_paid_in_lieu=data.notice_is_paid_in_lieu,
            notice_value=None if data.notice_band is None else data.notice_band.notice_value,
            notice_unit=None if data.notice_band is None else data.notice_band.notice_unit.value,
            accommodation_offset=data.accommodation_offset,
            leave_due_days=data.leave_due_days,
            leave_due_hours=data.leave_due_hours,
            incomplete_cycle_days=data.incomplete_cycle_days,
            incomplete_cycle_hours=data.incomplete_cycle_hours,
            days_worked_in_incomplete_cycle=data.days_worked_in_incomplete_cycle,
            months_of_service=data.months_of_service,
            completed_years_of_service=data.completed_years_of_service,
            dismissed_for_operational_requirements=data.dismissed_for_operational_requirements,
            unreasonably_refused_alternative_employment=(
                data.unreasonably_refused_alternative_employment
            ),
            negative_leave_balance=data.negative_leave_balance,
        ),
        statutory_rows=rows_of(*provenance),
        outputs=as_text(
            notice_pay=(Money.of(ZERO) if notice is None else notice.amount).exact,
            leave_due_pay=leave_due.exact,
            pro_rata_leave_pay=pro_rata.exact,
            severance=(Money.of(ZERO) if severance is None else severance.amount).exact,
            total=total.exact,
            used_the_average=rates.used_the_average,
        ),
        warnings=tuple(warnings),
    )

    return TerminationResult(
        lines=tuple(lines),
        total=total,
        notice_pay=Money.of(ZERO) if notice is None else notice.amount,
        leave_due_pay=leave_due,
        pro_rata_leave_pay=pro_rata,
        severance=Money.of(ZERO) if severance is None else severance.amount,
        rates=rates,
        trace=trace,
    )
