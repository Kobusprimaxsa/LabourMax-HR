"""P7 chunk 8b: a run calculated end to end, from rows to a finalised payslip.

Runs on the SHIPPED reference data — the 2027 tax tables, the UIF and SDL
parameters, the rule sets, the public holiday calendar and the component
catalogue, loaded through the real loader in dependency order — so a figure here
is what a real June 2026 payroll would produce. Every expected figure is worked
by hand in the test's docstring; none is read back from the code.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from attendance.models import AttendanceDay
from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.engagements import engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeRemuneration, EmployeeTaxProfile
from employers.components import seed_system_components
from employers.models import Employer, PayGroup
from payroll import assembly, runs, validation, ytd
from payroll.models import PayPeriod, PayrollCalculationTrace, PayrollRun
from statutory.loader import load_reference_data
from statutory.models import ReferenceDataVersion, Sector, TaxYear

pytestmark = pytest.mark.django_db

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"
LOADED = (
    "ref-2026.03.01.json",
    "ref-2026.03.01-holidays.json",
    "ref-2026.03.01-rules.json",
    "ref-2026.03.01-sd1.json",
    "ref-2026.03.01-codes.json",
    "ref-2026.03.01-lumpsum.json",
    "ref-2026.03.01-employment.json",
    "ref-2026.03.01-remuneration.json",
)
JUNE = (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30))
_ids = iter(range(200, 999))


def load_shipped_reference() -> TaxYear:
    """The shipped reference data through the real loader, and the catalogue."""
    for name in LOADED:
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))
    seed_system_components()
    return TaxYear.objects.get(label="2026/2027")


@pytest.fixture
def reference(db):
    return load_shipped_reference()


def an_employer(sector_code, name="Employer"):
    tenant = Tenant.objects.create(trading_name=name)
    sector = Sector.objects.get(code=sector_code)
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(tenant=tenant, trading_name=name, sector=sector)
        group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Monthly",
            pay_frequency=PayGroup.PayFrequency.MONTHLY,
            first_period_start=datetime.date(2026, 3, 1),
        )
    return employer, group


def a_june_period(group, tax_year, working_days="22.000"):
    with tenant_context(group.tenant_id):
        return PayPeriod.objects.create(
            tenant=group.tenant,
            pay_group=group,
            tax_year=tax_year,
            period_number=4,
            period_start=JUNE[0],
            period_end=JUNE[1],
            payment_date=JUNE[1],
            working_days_in_period=Decimal(working_days),
        )


def an_employee(
    employer,
    group,
    *,
    basis,
    rate,
    hourly,
    daily,
    start=datetime.date(2025, 1, 6),
    tax_profile=True,
):
    n = next(_ids)
    body = f"8506155{n:03d}08"[:12]
    with tenant_context(employer.tenant_id):
        employee = Employee.objects.create(
            tenant=employer.tenant,
            employer=employer,
            first_name="Zanele",
            last_name=f"Khumalo{n}",
            date_of_birth=datetime.date(1985, 6, 15),
            mobile_number=f"+2782100{n:04d}",
            email=f"zanele{n}@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
    engagement = engage(employee, start_date=start, job_title="Worker")
    with tenant_context(employer.tenant_id):
        EmployeeRemuneration.objects.create(
            tenant=employer.tenant,
            employee=employee,
            engagement=engagement,
            pay_group=group,
            pay_basis=basis,
            rate_amount=Decimal(rate),
            derived_hourly_rate=Decimal(hourly),
            derived_daily_rate=Decimal(daily),
            derived_monthly_rate=Decimal(rate) if basis == "monthly" else Decimal("0"),
            effective_from=start,
        )
        if tax_profile:
            EmployeeTaxProfile.objects.create(
                tenant=employer.tenant,
                employee=employee,
                tax_status="standard",
                effective_from=start,
            )
    return employee


def a_worked_day(employee, day, **buckets):
    values = {
        "ordinary_hours": Decimal("0"),
        "overtime_hours": Decimal("0"),
        "sunday_hours": Decimal("0"),
        "public_holiday_hours": Decimal("0"),
        "night_hours": Decimal("0"),
        "paid_hours_guaranteed": Decimal("0"),
        "standby_hours_worked": Decimal("0"),
        "days_worked_equivalent": Decimal("1.000"),
    }
    values.update(buckets)
    with tenant_context(employee.tenant_id):
        return AttendanceDay.objects.create(
            tenant=employee.tenant, employee=employee, work_date=day, **values
        )


def calculated(employer, period):
    run = runs.open_run(period)
    return runs.calculate(run)


def only_payslip(run):
    with tenant_context(run.tenant_id):
        (payslip,) = run.payslips.all()
        lines = {line.component_code: line for line in payslip.lines.all()}
    return payslip, lines


# ================================================================ the figures


def test_a_domestic_workers_june_payslip(reference):
    """A domestic worker on R5 000 a month, June 2026.

    PAYE: R5 000 × 12 = R60 000 a year, under the R99 000 threshold — nil.
    UIF: 1% of R5 000 = R50,00 each side, the ceiling (R17 712) not reached.
    SDL: a household captures no SDL registration — nil, and said.
    Net: R5 000 − R50 = R4 950,00.
    """
    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    an_employee(
        employer, group, basis="monthly", rate="5000.00", hourly="25.641026", daily="230.769231"
    )
    run = calculated(employer, a_june_period(group, reference))

    payslip, lines = only_payslip(run)
    assert run.status == PayrollRun.Status.CALCULATED
    assert set(lines) == {"BASIC", "UIF_EE", "UIF_ER"}
    assert lines["BASIC"].amount == Decimal("5000.00")
    assert (lines["BASIC"].source_code, lines["BASIC"].component_type) == ("3601", "earning")
    assert (payslip.paye, payslip.uif_employee, payslip.uif_employer) == (
        Decimal("0.00"),
        Decimal("50.00"),
        Decimal("50.00"),
    )
    assert payslip.sdl_employer == Decimal("0.00")
    assert (payslip.total_earnings, payslip.total_deductions, payslip.net_pay) == (
        Decimal("5000.00"),
        Decimal("50.00"),
        Decimal("4950.00"),
    )
    assert payslip.total_employer_contributions == Decimal("50.00")
    assert (run.employee_count, run.total_gross, run.total_net_pay) == (
        1,
        Decimal("5000.00"),
        Decimal("4950.00"),
    )
    assert run.total_employer_cost == Decimal("5050.00")

    with tenant_context(run.tenant_id):
        traces = list(PayrollCalculationTrace.objects.filter(payslip=payslip).order_by("sequence"))
    assert [t.calculator_name for t in traces] == [
        "gross.gross_pay",
        "paye.employees_tax",
        "uif.contribution",
        "sdl.levy",
        "net.net_pay",
    ]
    assert [t.sequence for t in traces] == [1, 2, 3, 4, 5]

    issues = {i.issue_code for i in validation.validate(run)}
    assert "no_sdl_registration" in issues
    assert "no_bank_account" in issues


def test_an_hourly_cleaner_with_overtime(reference):
    """A contract cleaner at R33,27 an hour (SD1 Area A), two nine-hour days
    with two hours' overtime each.

    BASIC: 18 × 33,27 = R598,86.  OT: 4 × 33,27 × 1,5 = R199,62 (SD1's 1,5).
    Gross R798,48. PAYE nil. UIF: 1% of 798,48 = 7,9848 → R7,98 each side.
    Net: 798,48 − 7,98 = R790,50.
    """
    employer, group = an_employer(Sector.Code.CONTRACT_CLEANING, "Cleaners")
    cleaner = an_employee(
        employer, group, basis="hourly", rate="33.27", hourly="33.270000", daily="299.430000"
    )
    for day in (datetime.date(2026, 6, 2), datetime.date(2026, 6, 3)):
        a_worked_day(cleaner, day, ordinary_hours=Decimal("9.00"), overtime_hours=Decimal("2.00"))

    payslip, lines = only_payslip(calculated(employer, a_june_period(group, reference)))

    assert lines["BASIC"].amount == Decimal("598.86")
    assert lines["OT_1_5"].amount == Decimal("199.62")
    assert lines["OT_1_5"].source_code == "3607"
    assert payslip.total_earnings == Decimal("798.48")
    assert (payslip.ordinary_hours, payslip.overtime_hours) == (Decimal("18.000"), Decimal("4.000"))
    assert payslip.uif_employee == Decimal("7.98")
    assert payslip.net_pay == Decimal("790.50")


def test_a_mid_month_starter_is_paid_for_the_days_employed(reference):
    """Engaged 16 June 2026 on R5 000 a month. June has 22 weekdays; 1 to 15
    June holds 11 of them, unpaid because not yet employed (D-293).

    BASIC: 5 000 × (22 − 11) ÷ 22 = R2 500,00.
    PAYE: periods worked 15 ÷ 30 = 0,5, annual equivalent 2 500 ÷ 0,5 × 12 =
    R60 000 — nil. UIF: R25,00.
    """
    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    an_employee(
        employer,
        group,
        basis="monthly",
        rate="5000.00",
        hourly="25.641026",
        daily="230.769231",
        start=datetime.date(2026, 6, 16),
    )

    run = calculated(employer, a_june_period(group, reference))
    payslip, lines = only_payslip(run)

    assert lines["BASIC"].amount == Decimal("2500.00")
    assert payslip.uif_employee == Decimal("25.00")
    with tenant_context(run.tenant_id):
        paye = PayrollCalculationTrace.objects.get(
            payslip=payslip, calculator_name="paye.employees_tax"
        )
    assert paye.inputs["periods_worked"] == "0.5"
    assert paye.outputs["annual_equivalent"] == "60000.000000"


# ================================================================ refusals


def test_an_employee_with_no_tax_profile_gets_no_payslip_and_a_blocking_issue(reference):
    """D-292: the run calculates everybody else and names who it could not pay."""
    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    an_employee(
        employer, group, basis="monthly", rate="5000.00", hourly="25.641026", daily="230.769231"
    )
    missing = an_employee(
        employer,
        group,
        basis="monthly",
        rate="4000.00",
        hourly="20.512821",
        daily="184.615385",
        tax_profile=False,
    )

    run = calculated(employer, a_june_period(group, reference))
    issues = [i for i in validation.validate(run) if i.employee_id == missing.pk]

    assert run.status == PayrollRun.Status.CALCULATED
    assert run.employee_count == 1
    (issue,) = issues
    assert issue.issue_code == "not_priced:no_tax_profile"
    assert issue.severity == "error"
    assert "No tax profile is in force on 30 June 2026" in issue.message


def test_a_worked_sunday_that_is_also_a_public_holiday_refuses(reference):
    """D-291: 9 August 2026 is a Sunday AND a public holiday (D-280), and which
    of s16 and s18 prices it is O-40's question."""
    employer, group = an_employer(Sector.Code.CONTRACT_CLEANING, "Cleaners")
    cleaner = an_employee(
        employer, group, basis="hourly", rate="33.27", hourly="33.270000", daily="299.430000"
    )
    a_worked_day(cleaner, datetime.date(2026, 8, 9), sunday_hours=Decimal("4.00"))
    with tenant_context(group.tenant_id):
        period = PayPeriod.objects.create(
            tenant=group.tenant,
            pay_group=group,
            tax_year=reference,
            period_number=6,
            period_start=datetime.date(2026, 8, 1),
            period_end=datetime.date(2026, 8, 31),
            payment_date=datetime.date(2026, 8, 31),
            working_days_in_period=Decimal("21.000"),
        )

    run = calculated(employer, period)
    (issue,) = [i for i in validation.validate(run) if i.employee_id == cleaner.pk]

    assert run.employee_count == 0
    assert issue.issue_code == "not_priced:sunday_public_holiday"
    assert "Sunday 09 August 2026" in issue.message
    assert "O-40" in issue.message


def test_the_refusal_reason_is_rederived_so_fixing_the_data_clears_it(reference):
    """The issue is derived (D-234): capture the tax profile and the same
    validation reports the employee as payable-but-not-yet-priced, and a
    recalculation pays them."""
    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    employee = an_employee(
        employer,
        group,
        basis="monthly",
        rate="5000.00",
        hourly="25.641026",
        daily="230.769231",
        tax_profile=False,
    )
    run = calculated(employer, a_june_period(group, reference))
    assert {i.issue_code for i in validation.validate(run)} >= {"not_priced:no_tax_profile"}

    with tenant_context(employer.tenant_id):
        EmployeeTaxProfile.objects.create(
            tenant=employer.tenant,
            employee=employee,
            tax_status="standard",
            effective_from=datetime.date(2025, 1, 6),
        )
    assert "not_priced:stale" in {i.issue_code for i in validation.validate(run)}

    run = runs.calculate(run)
    assert run.employee_count == 1
    assert not [i for i in validation.validate(run) if i.issue_code.startswith("not_priced")]


# ============================================================ recalculation


def test_recalculating_replaces_the_payslip_and_its_traces(reference):
    """D-294: a recalculation leaves one payslip per employee and only the
    traces of the calculation that stands."""
    employer, group = an_employer(Sector.Code.CONTRACT_CLEANING, "Cleaners")
    cleaner = an_employee(
        employer, group, basis="hourly", rate="33.27", hourly="33.270000", daily="299.430000"
    )
    day = a_worked_day(cleaner, datetime.date(2026, 6, 2), ordinary_hours=Decimal("9.00"))
    run = calculated(employer, a_june_period(group, reference))

    with tenant_context(employer.tenant_id):
        AttendanceDay.objects.filter(pk=day.pk).update(overtime_hours=Decimal("1.00"))
    run = runs.calculate(run)

    payslip, lines = only_payslip(run)
    assert lines["OT_1_5"].amount == Decimal("49.91"), "1 × 33,27 × 1,5 = 49,905"
    with tenant_context(employer.tenant_id):
        assert (
            PayrollCalculationTrace.objects.filter(
                employee=cleaner, calculator_name="gross.gross_pay"
            ).count()
            == 1
        )


# ============================================================ to finalised


def test_a_calculated_run_approves_finalises_and_reaches_the_year_to_date(reference):
    """The whole path, with the reference data verified the way D-288 verified
    it — by a person, golden tests passed, current through February 2027."""
    person = AppUser.objects.create_user(email="verifier@example.com", password="x" * 16)
    for version in ReferenceDataVersion.objects.all():
        version.verified_at = datetime.datetime(2026, 9, 24, tzinfo=datetime.UTC)
        version.verified_by_user = person
        version.golden_tests_passed = True
        version.data_current_through = datetime.date(2027, 2, 28)
        version.save()

    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    employee = an_employee(
        employer, group, basis="monthly", rate="5000.00", hourly="25.641026", daily="230.769231"
    )
    run = calculated(employer, a_june_period(group, reference))
    for issue in validation.validate(run):
        if issue.severity != "error":
            continue
        validation.resolve_issue(issue, resolved_by=person, reason="test")

    run = runs.approve(run, approved_by=person)
    runs.finalise(run, finalised_by=person)

    row = ytd.rebuild(employee, reference)[0]
    assert (row.ytd_gross, row.ytd_uif_employee, row.periods_processed) == (
        Decimal("5000.00"),
        Decimal("50.00"),
        1,
    )
    assert row.ytd_by_source_code == {"3601": "5000.00", "4141": "100.00"}


def test_employees_in_counts_only_those_engaged_on_the_pay_group(reference):
    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    here = an_employee(
        employer, group, basis="monthly", rate="5000.00", hourly="25.641026", daily="230.769231"
    )
    an_employee(
        employer,
        group,
        basis="monthly",
        rate="5000.00",
        hourly="25.641026",
        daily="230.769231",
        start=datetime.date(2026, 7, 1),
    )
    run = runs.open_run(a_june_period(group, reference))

    assert assembly.employees_in(run) == [here]
