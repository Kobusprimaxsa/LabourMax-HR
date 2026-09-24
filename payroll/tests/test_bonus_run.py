"""P7 chunk 8d: the December bonus run (D-311), on the SHIPPED reference data.

An SD1 contract cleaner at R80 an hour, 45 hours a week — R3 600 a week —
engaged 1 March 2026. SD1's cycle runs January to December and part months earn
nothing, so March to December is TEN full months.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from attendance.models import AttendanceDay
from calculators.paye import employees_tax
from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import terminate
from employees.models import EmployeeEngagement, EmployeeTaxProfile
from employers.models import Employer, PayGroup
from payroll import assembly, replay, runs, validation
from payroll.models import AnnualBonusCycle, PayPeriod, PayrollRun
from payroll.tests.test_termination_payslip import a_cleaner, a_cleaning_employer, load_shipped
from statutory import resolve
from statutory.models import Sector

pytestmark = pytest.mark.django_db

DECEMBER = (datetime.date(2026, 12, 1), datetime.date(2026, 12, 31))
HOLIDAYS = {datetime.date(2026, 12, 16), datetime.date(2026, 12, 25)}


@pytest.fixture
def shipped(db):
    return load_shipped()


def december(group, tax_year):
    with tenant_context(group.tenant_id):
        return PayPeriod.objects.create(
            tenant=group.tenant,
            pay_group=group,
            tax_year=tax_year,
            period_number=10,
            period_start=DECEMBER[0],
            period_end=DECEMBER[1],
            payment_date=DECEMBER[1],
            working_days_in_period=Decimal("23.000"),
        )


def a_cleaner_from_march(employer, group):
    return a_cleaner(employer, group, hourly="80.00", start=datetime.date(2026, 3, 1))


def twenty_days_in_december(employee):
    """The first twenty December weekdays that are not public holidays, nine
    ordinary hours each."""
    day, days = DECEMBER[0], 0
    with tenant_context(employee.tenant_id):
        while days < 20:
            if day.weekday() < 5 and day not in HOLIDAYS:
                AttendanceDay.objects.create(
                    tenant=employee.tenant,
                    employee=employee,
                    work_date=day,
                    ordinary_hours=Decimal("9.00"),
                    days_worked_equivalent=Decimal("1.000"),
                )
                days += 1
            day += datetime.timedelta(days=1)


def through(run, person):
    """Calculated to finalised, every blocking issue resolved by a named person."""
    for issue in validation.validate(run):
        if issue.severity == "error":
            validation.resolve_issue(issue, resolved_by=person, reason="test")
    runs.finalise(runs.approve(run, approved_by=person), finalised_by=person)
    return run


def only_payslip(run):
    with tenant_context(run.tenant_id):
        (payslip,) = run.payslips.all()
        lines = {line.component_code: line for line in payslip.lines.all()}
    return payslip, lines


def the_december_runs(shipped):
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, _ = a_cleaner_from_march(employer, group)
    twenty_days_in_december(employee)
    period = december(group, tax_year)
    regular = runs.open_run(period)
    bonus = runs.open_run(period, run_type=PayrollRun.RunType.BONUS)
    through(runs.calculate(regular), person)
    return employer, employee, period, regular, bonus, person


# =================================================================== the figures


def test_the_december_bonus_is_taxed_once_on_top_of_ordinary_pay(shipped):
    """REGULAR payslip: 20 days × 9 h × R80 = R14 400,00.
      annual equivalent 14 400 × 12 = R172 800; tax 18% = 31 104,00, less the
      primary rebate 17 820 = 13 284,00 a year; December's twelfth R1 107,00.
      UIF 1% × 14 400 = R144,00.

    BONUS payslip: SD1 3(3), 4,333 weeks × R3 600 × 10 ÷ 12 = R12 999,00.
      PAYE, the bonus added to the annual equivalent ONCE:
        tax on 172 800 + 12 999 = 185 799 at 18% = 33 443,82, less 17 820
        = 15 623,82; less the 13 284,00 the ordinary pay bears = R2 339,82.
      UIF: ordinary + bonus = R27 399 in the month, above the R17 712 ceiling,
        so 1% × 17 712 = 177,12, less the 144,00 already deducted = R33,12.
      NET 12 999,00 − 2 339,82 − 33,12 = R10 626,06.

    NOT TAXED TWICE: 1 107,00 + 2 339,82 = 3 446,82 is exactly the tax one
    calculation over ordinary pay and bonus together deducts in the month.
    """
    employer, employee, period, regular, bonus, person = the_december_runs(shipped)
    regular_payslip, _ = only_payslip(regular)
    assert (regular_payslip.total_earnings, regular_payslip.paye, regular_payslip.uif_employee) == (
        Decimal("14400.00"),
        Decimal("1107.00"),
        Decimal("144.00"),
    )

    bonus = runs.calculate(bonus)
    payslip, lines = only_payslip(bonus)

    assert set(lines) == {"BONUS_PRO_RATA", "PAYE", "UIF_EE", "UIF_ER"}
    assert lines["BONUS_PRO_RATA"].amount == Decimal("12999.00")
    assert (lines["BONUS_PRO_RATA"].units, lines["BONUS_PRO_RATA"].source_code) == (
        Decimal("10.0000"),
        "3605",
    )
    assert (payslip.paye, payslip.uif_employee, payslip.net_pay) == (
        Decimal("2339.82"),
        Decimal("33.12"),
        Decimal("10626.06"),
    )

    # The whole month, in one calculation, deducts exactly what the two did.
    with tenant_context(employee.tenant_id):
        profile = EmployeeTaxProfile.objects.get(employee=employee)
        together = employees_tax(
            assembly.paye_input(
                employee,
                profile,
                resolve.tax_year(DECEMBER[1]),
                calculated_for=DECEMBER[1],
                remuneration=Decimal("14400.00"),
                annual_payment=Decimal("12999.00"),
                periods_in_year=Decimal("12"),
                periods_worked=Decimal("1"),
            )
        )
    assert together.tax.rounded == regular_payslip.paye + payslip.paye == Decimal("3446.82")
    # And both reproduce from their own traces alone (D-313).
    assert replay.reproduce(regular_payslip) == []
    assert replay.reproduce(payslip) == []


def test_finalising_the_bonus_run_records_the_payment_and_closes_the_period(shipped):
    employer, employee, period, regular, bonus, person = the_december_runs(shipped)
    with tenant_context(employer.tenant_id):
        period.refresh_from_db()
    assert period.status == "in_progress", "the live bonus run keeps the period open (D-305)"

    through(runs.calculate(bonus), person)

    with tenant_context(employer.tenant_id):
        period.refresh_from_db()
        cycle = AnnualBonusCycle.objects.get(employee=employee)
    assert period.status == "closed"
    assert (cycle.status, cycle.paid_amount, cycle.paid_in_payroll_run_id) == (
        "paid",
        Decimal("12999.00"),
        bonus.pk,
    )


# =================================================================== refusals


def test_an_employer_with_no_bonus_gets_no_bonus_run(shipped):
    """PROVE EVERY GUARD FAILS: SD7 gives a domestic worker no bonus, and an
    employer's own bonus is never assumed — so there is no run to open."""
    tax_year, person = shipped
    tenant = Tenant.objects.create(trading_name="Household")
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant,
            trading_name="Household",
            sector=Sector.objects.get(code=Sector.Code.DOMESTIC),
        )
        group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=datetime.date(2026, 3, 1),
        )
    a_cleaner_from_march(employer, group)

    with pytest.raises(runs.PayrollRunError, match="Nobody on this pay group is owed a bonus"):
        runs.open_run(december(group, tax_year), run_type=PayrollRun.RunType.BONUS)


def test_the_bonus_is_not_priced_before_the_regular_payslip_is_final(shipped):
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, _ = a_cleaner_from_march(employer, group)
    twenty_days_in_december(employee)
    period = december(group, tax_year)
    runs.calculate(runs.open_run(period))

    bonus = runs.calculate(runs.open_run(period, run_type=PayrollRun.RunType.BONUS))

    assert bonus.employee_count == 0
    (issue,) = [i for i in validation.validate(bonus) if i.issue_code.startswith("not_priced")]
    assert issue.issue_code == "not_priced:bonus_before_regular_final"


def test_the_bonus_refuses_if_the_ordinary_tax_has_moved_since_the_regular_run(shipped):
    """PROVE EVERY GUARD FAILS: a medical scheme member captured after the
    regular run moves the ordinary pay's tax. Taxing the bonus against the new
    figure would leave the month's total wrong; it refuses instead."""
    employer, employee, period, regular, bonus, person = the_december_runs(shipped)
    with tenant_context(employer.tenant_id):
        EmployeeTaxProfile.objects.filter(employee=employee).update(medical_scheme_members=1)

    bonus = runs.calculate(bonus)

    (issue,) = [i for i in validation.validate(bonus) if i.issue_code.startswith("not_priced")]
    assert issue.issue_code == "not_priced:ordinary_tax_moved"
    assert "where the regular payslip deducted 1107.00" in issue.message


def test_a_leaver_is_left_out_of_the_bonus_run_and_o44_stays_open(shipped):
    """A cleaner who left on 30 November was paid their share on the
    termination payslip (D-308). Whether a long-serving leaver is owed the WHOLE
    bonus is O-44 and stays open: this build pays the pro-rata reading there,
    so here they are not owed one."""
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    stayer, _ = a_cleaner_from_march(employer, group)
    leaver, engagement = a_cleaner_from_march(employer, group)
    terminate(
        engagement,
        termination_date=datetime.date(2026, 11, 30),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        notice_worked=True,
    )
    from payroll.bonusrun import employees_owed

    assert employees_owed(december(group, tax_year)) == [stayer]
