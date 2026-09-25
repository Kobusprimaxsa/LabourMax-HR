"""The December bonus run — P7 chunk 8d (D-311).

A run of ``run_type = bonus`` prices the annual bonus and nothing else: one
``BONUS_PRO_RATA`` line (3605, the only bonus component sheet 02 carries) and the
PAYE, UIF and SDL that line attracts. The bonus itself is
``calculators/bonus.py``'s — the same formula that prices a leaver's share
(D-286), so the December payment and a June leaver's cannot come from two
formulas.

**Only a gazetted bonus.** SD1 clause 3(3) and the BCCCI's clause 4.5 are the
instruments that grant one, read off ``termination_rule_set``. An employer's own
bonus — a thirteenth cheque in a contract — has nowhere to be captured in this
build and is never assumed. **An employer whose employees are owed no bonus in
the period gets no run at all**: ``runs.open_run()`` refuses a bonus run over a
period in which nobody's cycle ends.

**PAYE is annualised ONCE** (D-212 to D-215). The bonus is the ``annual_payment``
on top of the employee's ordinary pay for the period, and that ordinary pay is
read from the FINALISED regular payslip's own PAYE trace — its remuneration, its
periods worked — never re-derived. The bonus payslip deducts only
``tax_on_annual_payment``. The calculator is asked for the ordinary pay's tax
in the same call, and a bonus run whose answer differs from what the regular
payslip deducted REFUSES: that is the proof the ordinary pay is not taxed twice,
and the guard against the rows having moved underneath it.

So a bonus run is opened BEFORE the regular run over the period is finalised
(finalising the last live run closes the period, D-305) and calculated AFTER it.

**UIF** is one month's contribution on ordinary pay and bonus together, capped
once at the monthly ceiling (UICA s6(2) — bonuses are remuneration, commission
is not), less what the regular payslip already deducted. **SDL** has no
ceiling, so the bonus's levy is the levy on the bonus.

**Leavers are not in it.** A leaver's share is paid on their termination payslip
(D-308) - PRO RATA on completed full calendar months of the current cycle,
however long their service (D-320, O-44 closed) - so the December run leaves
them out: that bonus has already been paid.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Q

from calculators.base import ZERO, Money
from calculators.bonus import BonusInputError, annual_bonus
from calculators.paye import PayeInputError, employees_tax
from calculators.sdl import SdlInput, levy
from calculators.uif import UifInput, contribution
from core.managers import tenant_context_of
from employees.models import EmployeeEngagement, EmployeeRemuneration, EmployeeTaxProfile
from payroll import bonus as bonuses
from payroll.models import (
    AnnualBonusCycle,
    PayPeriod,
    PayrollCalculationTrace,
    PayrollRun,
    Payslip,
)
from statutory import resolve


def owed_in(period: PayPeriod, employee) -> bool:
    """Is this employee's bonus cycle paid in this period?"""
    data = bonuses.bonus_input(employee, as_at=period.period_end)
    return (
        data is not None
        and not data.is_termination
        and period.period_start <= data.cycle_end <= period.period_end
    )


def employees_owed(period: PayPeriod) -> list:
    """Everybody on the pay group whose bonus cycle ends in the period."""
    from payroll.assembly import employees_in_period

    return [employee for employee in employees_in_period(period) if owed_in(period, employee)]


def _regular_payslip(period: PayPeriod, employee) -> Payslip | None:
    """The finalised payslip for the DAYS of this period — not a bonus run's,
    not a reversal, not one a reversal has since cancelled."""
    return (
        Payslip.objects.filter(
            employee=employee,
            pay_period=period,
            is_finalised=True,
            is_reversal=False,
            payroll_run__status=PayrollRun.Status.FINALISED,
        )
        .exclude(payroll_run__run_type=PayrollRun.RunType.BONUS)
        .order_by("-payroll_run__run_number")
        .first()
    )


def build(run: PayrollRun, employee):
    """Price one employee's bonus payslip. Reads; never writes."""
    from payroll.assembly import (
        SDL_RATE,
        UIF_CEILING,
        UIF_EMPLOYEE_RATE,
        UIF_EMPLOYER_RATE,
        CannotPrice,
        Draft,
        DraftLine,
        _component,
        _figure,
        _in_force,
        paye_input,
        sdl_liability,
    )

    period = run.pay_period
    end = period.period_end
    with tenant_context_of(employee):
        data = bonuses.bonus_input(employee, as_at=end)
        if data is None or not (period.period_start <= data.cycle_end <= end):
            raise CannotPrice(
                "no_bonus_this_period",
                "No bonus cycle of this employee's ends in this period.",
            )
        if data.is_termination:
            raise CannotPrice(
                "bonus_paid_on_termination",
                "This employee has left; their share is paid on the termination payslip "
                "(D-308). A leaver is paid pro rata, however long their service (D-320).",
            )
        paid = AnnualBonusCycle.objects.filter(
            employee=employee,
            cycle_start=data.cycle_start,
            status__in=AnnualBonusCycle.PAID_STATUSES,
        ).first()
        if paid is not None:
            raise CannotPrice(
                "bonus_already_paid",
                f"The bonus for the cycle from {data.cycle_start:%d %B %Y} was paid by run "
                f"{paid.paid_in_payroll_run_id}.",
            )
        try:
            bonus = annual_bonus(data)
        except BonusInputError as refused:
            raise CannotPrice("bonus_refused", str(refused)) from refused

        regular = _regular_payslip(period, employee)
        if regular is None:
            raise CannotPrice(
                "bonus_before_regular_final",
                "The bonus is taxed on top of the ordinary pay for the period (an annual "
                "payment, added once), so the regular payslip must be FINALISED first. "
                "Finalise the regular run, then calculate this one.",
            )
        ordinary = PayrollCalculationTrace.objects.get(
            payslip=regular, calculator_name="paye.employees_tax"
        )
        if Decimal(ordinary.inputs["annual_payment"]):
            raise CannotPrice(
                "bonus_after_annual_payment",
                f"The regular payslip already carries an annual payment of "
                f"{ordinary.inputs['annual_payment']}. Pricing a second on top is not built.",
            )
        profile = _in_force(EmployeeTaxProfile.objects.filter(employee=employee), end)
        engagement = (
            EmployeeEngagement.objects.filter(employee=employee, start_date__lte=end)
            .filter(Q(termination_date__isnull=True) | Q(termination_date__gte=end))
            .order_by("-start_date")
            .first()
        )
        remuneration = _in_force(
            EmployeeRemuneration.objects.filter(employee=employee, engagement=engagement), end
        )

        year = resolve.tax_year(period.payment_date)
        try:
            paye = employees_tax(
                paye_input(
                    employee,
                    profile,
                    year,
                    calculated_for=end,
                    remuneration=Decimal(ordinary.inputs["remuneration"]),
                    annual_payment=bonus.amount.rounded,
                    periods_in_year=Decimal(ordinary.inputs["periods_in_year"]),
                    periods_worked=Decimal(ordinary.inputs["periods_worked"]),
                )
            )
        except PayeInputError as refused:
            raise CannotPrice("paye_refused", str(refused)) from refused
        deducted = Money.of(Decimal(ordinary.outputs["tax_on_remuneration"]))
        if paye.tax_on_remuneration.exact != deducted.exact:
            raise CannotPrice(
                "ordinary_tax_moved",
                f"The ordinary pay's tax is now {paye.tax_on_remuneration} where the "
                f"regular payslip deducted {deducted}. A tax table or the tax profile moved "
                f"after the regular run; the bonus would be taxed against a different ordinary "
                f"figure than was deducted.",
            )

        uif_base = regular.uif_remuneration + bonus.amount.rounded
        uif = contribution(
            UifInput(
                calculated_for=end,
                remuneration=uif_base,
                commission=ZERO,
                excluded_remuneration=ZERO,
                monthly_ceiling=_figure(UIF_CEILING, end),
                employee_rate_percent=_figure(UIF_EMPLOYEE_RATE, end),
                employer_rate_percent=_figure(UIF_EMPLOYER_RATE, end),
                is_exempt=profile.is_uif_exempt,
                exemption_reason=profile.uif_exempt_reason or "",
            )
        )
        uif_employee = Money.of(uif.employee.rounded - regular.uif_employee)
        uif_employer = Money.of(uif.employer.rounded - regular.uif_employer)
        liable, reason = sdl_liability(employee, profile, end)
        sdl = levy(
            SdlInput(
                calculated_for=end,
                leviable_amount=bonus.amount.rounded,
                rate_percent=_figure(SDL_RATE, end),
                employer_is_liable=liable,
                exemption_reason=reason,
            )
        )

        uif_note = (
            f"1% of {uif.contribution_base} (ordinary {regular.uif_remuneration} + bonus), less "
            f"{regular.uif_employee} deducted on payslip {regular.payslip_number}"
        )
        lines = [
            DraftLine(
                component=_component("BONUS_PRO_RATA"),
                description="Annual bonus",
                amount=bonus.amount,
                units=Decimal(bonus.full_months),
                unit_type="months",
                rate=bonus.amount.exact / bonus.full_months,
                note=f"{data.rule.weeks} weeks x {bonus.full_months}/12, cycle to {data.cycle_end}",
            )
        ]
        for code, description, amount, note in (
            ("PAYE", "PAYE on the bonus", paye.tax_on_annual_payment, "annual payment, added once"),
            ("UIF_EE", "UIF (employee)", uif_employee, uif_note),
            ("UIF_ER", "UIF (employer)", uif_employer, uif_note),
            ("SDL_ER", "Skills Development Levy", sdl.levy, ""),
        ):
            if amount.exact:
                lines.append(
                    DraftLine(
                        component=_component(code),
                        description=description,
                        amount=amount,
                        note=note,
                    )
                )

        gross = bonus.amount.rounded
        deductions = paye.tax_on_annual_payment.rounded + uif_employee.rounded
        return Draft(
            employee=employee,
            engagement=engagement,
            remuneration=remuneration,
            bank_account=regular.bank_account,
            lines=lines,
            traces=[bonus.trace, paye.trace, uif.trace, sdl.trace],
            header={
                "is_termination_payslip": False,
                "pay_basis": remuneration.pay_basis,
                "rate_used": remuneration.rate_amount,
                "ordinary_hours": ZERO,
                "overtime_hours": ZERO,
                "days_worked": ZERO,
                "gross_remuneration": gross,
                "taxable_remuneration": gross,
                "uif_remuneration": uif.contribution_base.rounded - regular.uif_remuneration,
                "sdl_remuneration": sdl.leviable_amount.rounded,
                "paye": paye.tax_on_annual_payment.rounded,
                "uif_employee": uif_employee.rounded,
                "uif_employer": uif_employer.rounded,
                "sdl_employer": sdl.levy.rounded,
                "total_earnings": gross,
                "total_deductions": deductions,
                "total_employer_contributions": uif_employer.rounded + sdl.levy.rounded,
                "net_pay": gross - deductions,
                "payment_method": regular.payment_method,
            },
        )
