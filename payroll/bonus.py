"""The annual bonus cycle: resolve the rule, assemble the input, keep the cache.

The caller's half of ``calculators/bonus.py`` (D-286). Three things, and nothing
that pays anybody — the December payment and the ``paid`` / ``pro_rata_paid``
transitions belong to a payroll run, which is chunk 8's and blocked:

* ``bonus_input()`` — what the calculator needs for one employee as at one
  date, or **None when the instrument governing them gives no bonus**. The
  domestic sector and the BCEA carry no payment month on their
  ``termination_rule_set``, and None is how that reaches every caller.
* ``accrue()`` — the monthly accrual. Rebuilds each ``annual_bonus_cycle`` row
  from scratch (invariant 3, D-153's lesson: an incrementally maintained cache
  that has drifted cannot be told from a correct one). **An employer with no
  bonus gets no rows at all**, not rows of nil.
* ``termination_bonus()`` — the same input as at a termination date, for
  ``calculators/termination.py``, closing D-224's gap.

**The BCCCI elections are read only where the BCCCI governs.** Clause 4.5(g)
makes three of clause 4.5's rules minimums an employer may improve on (D-242),
and they are employer settings. SD1 grants no such discretion, so for an SD1
employee the gazetted position applies whatever the settings say: part months
earn nothing, the wage is "the employee's weekly wage", and "all employees"
qualify. The agreement is recognised by its rule set being AREA-scoped (D-240)
— it is the only instrument this build loads that way, and
``payroll/tests/test_bonus.py`` fails the day a second one arrives.
"""

from __future__ import annotations

import datetime

from django.db import transaction

from calculators.bonus import (
    BonusInput,
    BonusRule,
    RateBasis,
    WeeklyWage,
    annual_bonus,
    cycle_containing,
)
from core.managers import tenant_context_of
from employees.models import EmployeeEngagement, EmployeeRemuneration
from employees.remuneration import weekly_wage
from employers.onboarding import setting_value
from leave.cycles import _sector_area_of
from payroll.models import AnnualBonusCycle
from statutory import resolve


def rules_for(employee, on_date: datetime.date):
    return resolve.termination_rules(
        employee.employer.sector, on_date, _sector_area_of(employee, on_date)
    )


def bonus_rule(row) -> BonusRule:
    return BonusRule(
        weeks=row.annual_bonus_weeks,
        payment_month=row.annual_bonus_month,
        pro_rata_on_termination=row.annual_bonus_pro_rata_on_termination,
        min_service_months=row.annual_bonus_min_service_months,
        table="termination_rule_set",
        row_id=row.pk,
    )


def _engagement_for(employee, cycle_start, as_at) -> EmployeeEngagement | None:
    """The latest engagement overlapping the cycle and begun by ``as_at``. A
    re-hire starts fresh (D-103, D-163): months from an earlier engagement in
    the same cycle are not carried across, which is O-26's s84 question."""
    return (
        EmployeeEngagement.objects.filter(employee=employee, start_date__lte=as_at)
        .exclude(termination_date__lt=cycle_start)
        .order_by("-start_date")
        .first()
    )


def _wages(engagement) -> tuple[WeeklyWage, ...]:
    rows = EmployeeRemuneration.objects.filter(engagement=engagement).order_by("effective_from")
    return tuple(WeeklyWage(row.effective_from, row.effective_to, weekly_wage(row)) for row in rows)


def _elections(employee, rules, engagement) -> dict:
    if rules.sector_area_id is None:
        return {
            "part_first_month_counts": False,
            "rate_basis": RateBasis.AT_THE_END,
            "qualifies": True,
            "disqualified_because": "",
        }
    employer = employee.employer
    casual = engagement.contract_type == EmployeeEngagement.ContractType.CASUAL
    casuals_qualify = setting_value(employer, "BONUS_CASUALS_QUALIFY")
    return {
        "part_first_month_counts": not setting_value(employer, "BONUS_PART_MONTH_EARNS_NOTHING"),
        "rate_basis": (
            RateBasis.EACH_MONTH
            if setting_value(employer, "BONUS_RATE_BASIS") == "prevailing_each_month"
            else RateBasis.AT_THE_END
        ),
        "qualifies": not casual or casuals_qualify,
        "disqualified_because": (
            "a casual employee, BCCCI clause 4.5(f)" if casual and not casuals_qualify else ""
        ),
    }


def bonus_input(employee, *, as_at: datetime.date) -> BonusInput | None:
    """The calculator's input for one employee as at one date, or None where no
    bonus applies — no instrument bonus, or no engagement in the cycle."""
    with tenant_context_of(employee):
        rules = rules_for(employee, as_at)
        rule = bonus_rule(rules)
        if not rule.gives_a_bonus:
            return None
        cycle_start, cycle_end = cycle_containing(as_at, rule.payment_month)
        engagement = _engagement_for(employee, cycle_start, as_at)
        if engagement is None:
            return None
        left = engagement.termination_date
        service_end = left if left is not None and left <= as_at else None
        return BonusInput(
            calculated_for=as_at,
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            service_start=engagement.start_date,
            service_end=service_end,
            as_at=service_end or as_at,
            wages=_wages(engagement),
            is_termination=service_end is not None,
            **_elections(employee, rules, engagement),
        )


def termination_bonus(employee, termination_date: datetime.date) -> BonusInput | None:
    """The pro-rata bonus input for a leaver, for ``TerminationInput.annual_bonus``.
    The engagement must already carry the termination date."""
    return bonus_input(employee, as_at=termination_date)


def accrue(employer, *, as_at: datetime.date) -> list[AnnualBonusCycle]:
    """Rebuild every employee's current cycle row as at ``as_at``.

    Idempotent: run it twice on one date and the second changes nothing. Run
    it after a back-dated increase and the row moves, because it is recomputed
    from the remuneration history, never nudged. A row already paid is a record
    of a payment and is left alone.
    """
    from employees.models import Employee

    written = []
    with transaction.atomic(), tenant_context_of(employer):
        for employee in Employee.objects.filter(employer=employer).order_by("pk"):
            data = bonus_input(employee, as_at=as_at)
            if data is None:
                continue
            result = annual_bonus(data)
            existing = AnnualBonusCycle.objects.filter(
                employee=employee, cycle_start=data.cycle_start
            ).first()
            if existing is not None and existing.status != AnnualBonusCycle.Status.ACCRUING:
                continue
            row, _ = AnnualBonusCycle.objects.update_or_create(
                # The manager does not inject the tenant into update_or_create
                # (CLAUDE.md), so it is named in the lookup.
                tenant=employee.tenant,
                employee=employee,
                cycle_start=data.cycle_start,
                defaults={
                    "cycle_end": data.cycle_end,
                    "bonus_weeks": data.rule.weeks,
                    "full_months_worked": result.full_months,
                    "accrued_amount_exact": result.amount.exact,
                    "accrued_amount": result.amount.rounded,
                    "accrued_as_at": data.as_at,
                },
            )
            written.append(row)
    return written
