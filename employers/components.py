"""The system payroll components — the catalogue every payslip is assembled from.

Sixteen components, named in sheet 02 of the Database Specification. They are
**shared**: one row each, no tenant, readable by every employer on the platform and
writable by none of them. An employer that needs a transport allowance or a loan
repayment adds its own rows alongside.

Three rules hold for all sixteen, and each exists because of a specific way this
table could go wrong.

**No component carries a rate.**
    ``default_rate_multiplier`` and ``percentage_value`` are NULL on every system
    component and ``calculation_method`` is ``statutory``. The overtime multiplier,
    both Sunday multipliers, the public holiday multiplier and the SD1 night
    allowance percentage are gazetted figures with effective dates — they live in
    ``working_time_rule_set`` and are read through ``statutory.resolve`` when the
    payslip is calculated. The component's name says ``OT_1_5`` because that is what
    the workbook calls it and what an employer recognises on a payslip; the 1.5 is
    not stored here, and a March gazette that moved it would reach the calculation
    without anybody touching this file (D-88).

    That is also why the code deliberately does **not** read ``1.5`` from the code
    string. A component code is a label. A multiplier is a number somebody gazetted.

**The tax treatment is not decided here.**
    Each component points at a SARS source code, and the four base flags are copied
    from it rather than chosen. ``PayrollComponent.clean()`` refuses any other
    combination, so the reasoning stays in ``sars_source_code`` where it is cited
    and auditable, and the copy cannot drift (D-89).

**Leave pay follows the determination, not intuition.**
    ``affects_leave_pay_average`` implements the determination made under BCEA
    s35(5) — Government Notice 691 in Government Gazette 24889, in force 1 July
    2003. Its rule is a catch-all: **any cash payment** to the employee forms part
    of remuneration except those on a short exclusion list (payments enabling work,
    relocation, gratuities, share schemes, discretionary payments unrelated to hours
    or performance, entertainment, education). Overtime, Sunday work, public holiday
    work, night allowances and standby are none of those, so they are in.

    The one component that is not obvious is ``BONUS_PRO_RATA``; see its entry.

Two are not fully settled and say so, rather than being quietly guessed:

- ``SEVERANCE`` ships **inactive**. Severance benefits are source code 3901, which
  is not in the loaded reference data, and they are taxed on a directive rather than
  through the ordinary tables. Pointing it at 3601 would put a termination payment
  on the wrong IRP5 line, so it carries no code and cannot be used until 3901 is
  loaded and the directive handling is built in P6.
- ``ACCOM_DED`` is a deduction, so it carries no leave-pay flag — but the
  determination *includes* accommodation received as a benefit in kind in
  remuneration. That is an earnings-side fringe benefit this catalogue does not yet
  have, and it belongs with the fringe benefit work rather than here.

Seeding writes shared rows, which requires ``platform_context()`` — the same named,
logged, greppable hole used everywhere else that crosses a tenant boundary. It is
idempotent and never updates: a component that already exists is left exactly as it
is, because the system-row lock would refuse the write anyway and because a catalogue
row that a finalised payslip line points at must not change under it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from core.managers import platform_context
from employers.models import PayrollComponent

EARNING = PayrollComponent.ComponentType.EARNING
DEDUCTION = PayrollComponent.ComponentType.DEDUCTION
EMPLOYER_CONTRIBUTION = PayrollComponent.ComponentType.EMPLOYER_CONTRIBUTION
STATUTORY = PayrollComponent.CalculationMethod.STATUTORY
RATE_X_UNITS = PayrollComponent.CalculationMethod.RATE_X_UNITS
FIXED = PayrollComponent.CalculationMethod.FIXED


@dataclass(frozen=True)
class SystemComponent:
    """One catalogue entry, and why it is treated the way it is.

    ``reason`` is not decoration. Every field below it is a compliance decision, and
    a decision with no recorded reason is one nobody dares change in four years.
    """

    code: str
    name: str
    component_type: str
    calculation_method: str
    display_order: int
    reason: str
    source_code: str | None = None
    affects_leave_pay_average: bool = False
    is_active: bool = True
    #: Only for a component with no source code, where the flags stand alone.
    standalone_flags: dict[str, bool] = field(default_factory=dict)


SYSTEM_COMPONENTS: tuple[SystemComponent, ...] = (
    # ------------------------------------------------------------------ earnings
    SystemComponent(
        code="BASIC",
        name="Basic wage",
        component_type=EARNING,
        calculation_method=RATE_X_UNITS,
        display_order=10,
        source_code="3601",
        affects_leave_pay_average=True,
        reason=(
            "The ordinary wage. Rate × units rather than statutory: the rate is the "
            "employee's own, from the effective-dated remuneration row, and the "
            "minimum it may not fall below is checked against minimum_wage_rate "
            "separately."
        ),
    ),
    SystemComponent(
        code="OT_1_5",
        name="Overtime",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=20,
        source_code="3607",
        affects_leave_pay_average=True,
        reason=(
            "BCEA s10. The multiplier is working_time_rule_set.overtime_multiplier, "
            "which differs by sector and by date — the 1.5 in the code is a label."
        ),
    ),
    SystemComponent(
        code="SUNDAY_2_0",
        name="Sunday work",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=30,
        source_code="3601",
        affects_leave_pay_average=True,
        reason=(
            "BCEA s16, and the rule most often got wrong: double time when Sunday is "
            "NOT an ordinary working day for that employee, time and a half when it "
            "is. Two columns in working_time_rule_set, chosen per employee from the "
            "work schedule. One stored multiplier could only ever be half right."
        ),
    ),
    SystemComponent(
        code="PH_WORKED",
        name="Public holiday worked",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=40,
        source_code="3601",
        affects_leave_pay_average=True,
        reason=(
            "BCEA s18. Separate from the holiday an employee did not work, which is "
            "paid at the ordinary rate and appears as BASIC. A high earner above the "
            "BCEA threshold keeps this entitlement — they lose s18(3) only."
        ),
    ),
    SystemComponent(
        code="NIGHT_ALLOW",
        name="Night work allowance",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=50,
        source_code="3713",
        affects_leave_pay_average=True,
        reason=(
            "BCEA s17, and contract cleaning (SD1) is the only sector with a real "
            "gazetted figure: 10% of the hourly wage between 18:00 and 06:00. The "
            "percentage, the window and whether it is a percentage, a rand amount or "
            "time off are all columns on working_time_rule_set."
        ),
    ),
    SystemComponent(
        code="STANDBY",
        name="Standby allowance",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=60,
        source_code="3713",
        affects_leave_pay_average=True,
        reason=(
            "SD7 night standby, per shift. A cash payment for being available, not a "
            "payment enabling work, so the determination includes it in remuneration."
        ),
    ),
    SystemComponent(
        code="LEAVE_PAY",
        name="Annual leave pay",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=70,
        source_code="3601",
        reason=(
            "BCEA s21. affects_leave_pay_average is FALSE and must stay false: this "
            "is the output of the leave pay calculation, and feeding it back in would "
            "make a second period of leave compound off the first."
        ),
    ),
    SystemComponent(
        code="BONUS_PRO_RATA",
        name="Pro-rata annual bonus",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=80,
        source_code="3605",
        reason=(
            "SD1's 4.333-week December bonus, pro-rated as full calendar months over "
            "twelve on termination. Source code 3605 (annual payment), and the PAYE "
            "treatment is the classic December over-deduction: annualise regular pay "
            "×12 and add the annual payment ONCE, never ×12. "
            "affects_leave_pay_average is FALSE and the reading is flagged rather "
            "than settled: the determination's catch-all takes in any cash payment "
            "that is not discretionary, and a gazetted bonus is not discretionary — "
            "but averaging a once-a-year payment across thirteen weeks overstates a "
            "week's remuneration by roughly a third, and no source addresses the "
            "interaction. Confirm before the leave engine ships in P5."
        ),
    ),
    SystemComponent(
        code="SEVERANCE",
        name="Severance pay",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=90,
        source_code=None,
        is_active=False,
        standalone_flags={
            "is_taxable": False,
            "is_uif_base": False,
            "is_sdl_base": False,
            "is_coida_base": False,
        },
        reason=(
            "SHIPS INACTIVE, deliberately. BCEA s41: one week's remuneration per "
            "completed year of continuous service on operational-requirements "
            "dismissal. Severance benefits are source code 3901 and are taxed on a "
            "SARS directive against the retirement lump sum table, not through the "
            "ordinary PAYE tables. 3901 is not in the loaded reference data and the "
            "directive handling does not exist, so pointing this at 3601 would put a "
            "termination payment on the wrong IRP5 line and tax it at the wrong rate. "
            "The flags are all false because an inactive component must not look "
            "usable; they are set from 3901 when P6 loads it."
        ),
    ),
    SystemComponent(
        code="NOTICE_PAY",
        name="Payment instead of notice",
        component_type=EARNING,
        calculation_method=STATUTORY,
        display_order=100,
        source_code="3601",
        reason=(
            "BCEA s37(6). Ordinary remuneration for the notice period, paid instead "
            "of working it, so 3601 rather than a lump sum code. The notice length "
            "comes from termination_rule_set and departs from the BCEA in both "
            "sectors — four weeks from six months' service for domestic workers. "
            "affects_leave_pay_average is false: this is an output of the "
            "remuneration calculation, not an input to it."
        ),
    ),
    # ---------------------------------------------------------------- deductions
    SystemComponent(
        code="PAYE",
        name="PAYE",
        component_type=DEDUCTION,
        calculation_method=STATUTORY,
        display_order=200,
        source_code="4102",
        reason=(
            "Employees' tax. Brackets, rebates and the medical scheme fees tax credit "
            "all come from the tax year's reference rows. Directors use the ordinary "
            "tables — the flat 25% rate was repealed in 2017."
        ),
    ),
    SystemComponent(
        code="UIF_EE",
        name="UIF — employee contribution",
        component_type=DEDUCTION,
        calculation_method=STATUTORY,
        display_order=210,
        source_code="4141",
        reason=(
            "One percent of remuneration to the contribution ceiling. The ceiling "
            "moves on ministerial notice on NO fixed calendar, independent of the tax "
            "year, and is the parameter most often missed in South African payroll — "
            "which is precisely why it is a reference row and not a number here. "
            "Commission is excluded from the base; bonuses are not."
        ),
    ),
    SystemComponent(
        code="ACCOM_DED",
        name="Accommodation deduction",
        component_type=DEDUCTION,
        calculation_method=FIXED,
        display_order=220,
        source_code=None,
        standalone_flags={
            "is_taxable": False,
            "is_uif_base": False,
            "is_sdl_base": False,
            "is_coida_base": False,
        },
        reason=(
            "SD7 permits a deduction for accommodation supplied, capped at a "
            "percentage of the wage held in "
            "working_time_rule_set.accommodation_deduction_max_pct. A deduction from "
            "the wage rather than an IRP5 line, so no source code. Note the mirror "
            "image this catalogue does not yet have: accommodation received as a "
            "benefit in kind IS part of remuneration under the s35(5) determination, "
            "and that is an earnings-side fringe benefit belonging with P4."
        ),
    ),
    SystemComponent(
        code="ADVANCE_DED",
        name="Advance repayment",
        component_type=DEDUCTION,
        calculation_method=FIXED,
        display_order=230,
        source_code=None,
        standalone_flags={
            "is_taxable": False,
            "is_uif_base": False,
            "is_sdl_base": False,
            "is_coida_base": False,
        },
        reason=(
            "Recovery of money already advanced and already taxed when it was paid. "
            "No source code, and taxing it again would be taxing the same rand twice. "
            "BCEA s34 caps what may be deducted in total, and that cap is checked "
            "against the payslip rather than against this row."
        ),
    ),
    # ------------------------------------------------------ employer contributions
    SystemComponent(
        code="UIF_ER",
        name="UIF — employer contribution",
        component_type=EMPLOYER_CONTRIBUTION,
        calculation_method=STATUTORY,
        display_order=300,
        source_code="4141",
        reason=(
            "The employer's matching one percent, on the same ceiling. Not a "
            "deduction: it never reduces the employee's net pay, and a payslip that "
            "shows it as one is wrong. Same source code as UIF_EE — 4141 is the "
            "combined IRP5 line — which is why component_type rather than the code is "
            "what separates them."
        ),
    ),
    SystemComponent(
        code="SDL_ER",
        name="Skills Development Levy",
        component_type=EMPLOYER_CONTRIBUTION,
        calculation_method=STATUTORY,
        display_order=310,
        source_code="4142",
        reason=(
            "One percent of leviable remuneration. The R500,000 exemption is "
            "FORWARD-looking — it asks whether the employer reasonably believes "
            "payroll will exceed it over the next twelve months — so it cannot be "
            "computed from history and is a human-set flag on the employer's "
            "registration, not a threshold this component can test."
        ),
    ),
)


class ComponentSeedError(Exception):
    """The catalogue cannot be seeded. Nothing was written."""


def _source_codes(needed: set[str]):
    from statutory.models import SarsSourceCode

    found = {c.code: c for c in SarsSourceCode.objects.filter(code__in=needed)}
    missing = sorted(needed - set(found))
    if missing:
        raise ComponentSeedError(
            f"SARS source codes {missing} are not loaded, so the payroll components "
            f"that point at them cannot be created. Run "
            f"`python manage.py loadstatutory --all` first."
        )
    return found


def seed_system_components(*, activate_severance: bool = False) -> list[PayrollComponent]:
    """Create any system component that does not exist yet. Returns what was created.

    Idempotent, and deliberately never updates. A component already in the
    catalogue is left exactly as it is: finalised payslip lines point at these rows,
    and the system-row lock would refuse the write in any case. Changing a system
    component's treatment is a migration with a reason attached, not a re-seed.

    ``activate_severance`` exists so that P6 can turn SEVERANCE on in the same
    breath as loading source code 3901, without this module having to be edited by
    someone who has not read why it is off.

    **``transaction.atomic()`` is load-bearing and must stay outside the context
    block.** ``platform_context()`` pushes its flag into the database session with
    ``set_config(..., true)``, which is TRANSACTION-local — deliberately, so a
    pooled connection cannot carry one request's tenant into the next. Under
    autocommit, which is where every management command runs, each statement is its
    own transaction: the flag is set, the SELECT that follows commits, and the flag
    is gone before the first INSERT. That INSERT then fails with "new row violates
    row-level security policy for table payroll_component", which names the policy
    and says nothing about the missing context.

    This bit for real. The whole suite passed, because pytest-django wraps every
    test in a transaction and hides it, and the command failed on the first run
    against a live database. ``test_seeding_works_outside_a_wrapping_transaction``
    runs with ``transaction=True`` so that it cannot hide again.
    """
    wanted = {c.source_code for c in SYSTEM_COMPONENTS if c.source_code}
    codes = _source_codes(wanted)

    created: list[PayrollComponent] = []
    with transaction.atomic(), platform_context():
        existing = set(PayrollComponent.objects.shared().values_list("code", flat=True))

        for spec in SYSTEM_COMPONENTS:
            if spec.code in existing:
                continue

            if spec.source_code:
                code = codes[spec.source_code]
                flags = {
                    "is_taxable": code.is_taxable,
                    "is_uif_base": code.is_uif_remuneration,
                    "is_sdl_base": code.is_sdl_remuneration,
                    "is_coida_base": code.is_coida_remuneration,
                }
            else:
                code = None
                flags = dict(spec.standalone_flags)

            component = PayrollComponent(
                tenant=None,
                code=spec.code,
                name=spec.name,
                component_type=spec.component_type,
                calculation_method=spec.calculation_method,
                default_rate_multiplier=None,
                percentage_value=None,
                sars_source_code=code,
                affects_leave_pay_average=spec.affects_leave_pay_average,
                is_system=True,
                is_active=spec.is_active or (spec.code == "SEVERANCE" and activate_severance),
                display_order=spec.display_order,
                **flags,
            )
            component.full_clean(exclude=["tenant"])
            component.save()
            created.append(component)

    return created


def component(code: str) -> PayrollComponent:
    """Fetch one component by code, shared or the current tenant's own.

    Raises ``PayrollComponent.DoesNotExist`` rather than returning None: a payslip
    line with no component is not a thing that should be constructible.
    """
    return PayrollComponent.objects.get(code=code)
