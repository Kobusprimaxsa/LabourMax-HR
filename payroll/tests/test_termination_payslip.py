"""P7 chunk 8c: the termination payslip, on the SHIPPED reference data.

A leaver's final period carries the ordinary lines PLUS the reviewed
termination payout (D-308) — notice, leave due, pro-rata leave, the pro-rata
bonus — and the working is written out in the docstring of the test that proves
it, so a person can check every figure without running anything.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import transaction

from attendance.models import AttendanceDay
from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.engagements import engage, terminate
from employees.identity import luhn_check_digit
from employees.models import (
    Employee,
    EmployeeEngagement,
    EmployeeRemuneration,
    EmployeeTaxProfile,
    WorkSchedule,
    WorkScheduleDay,
)
from employers.components import seed_system_components
from employers.models import Employer, PayGroup
from leave.ledger import post_transaction
from leave.models import LeaveCycle, LeaveType
from leave.types import seed_system_leave_types
from payroll import runs, termination, validation
from payroll.models import (
    AnnualBonusCycle,
    PayPeriod,
    PayrollCalculationTrace,
    PayrollRun,
    TerminationPayout,
)
from statutory.loader import load_reference_directory
from statutory.models import ReferenceDataVersion, Sector, TaxYear

pytestmark = pytest.mark.django_db

ENGAGED = datetime.date(2025, 6, 1)
LEAVING = datetime.date(2026, 6, 12)  # a Friday
_ids = iter(range(300, 999))


@pytest.fixture
def shipped(db):
    return load_shipped()


def load_shipped():
    """Every shipped fixture, in dependency order, the way ``loadstatutory --all``
    loads them — the termination payout reads notice bands, the s40(c)
    qualifying period and the SD1 rule set, which the smaller payroll fixture
    set does not carry. Verified the way D-288 verified them."""
    load_reference_directory()
    seed_system_components()
    seed_system_leave_types()
    person = AppUser.objects.create_user(email="verifier@example.com", password="x" * 16)
    for version in ReferenceDataVersion.objects.all():
        version.verified_at = datetime.datetime(2026, 9, 24, tzinfo=datetime.UTC)
        version.verified_by_user = person
        version.golden_tests_passed = True
        version.data_current_through = datetime.date(2027, 2, 28)
        version.save()
    return TaxYear.objects.get(label="2026/2027"), person


def a_cleaning_employer():
    tenant = Tenant.objects.create(trading_name="Cleaners")
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant,
            trading_name="Cleaners",
            sector=Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING),
        )
        group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Cleaners",
            pay_frequency=PayGroup.PayFrequency.HOURLY,
            period_end_rule=PayGroup.PeriodEndRule.CALENDAR_MONTH_END,
            first_period_start=datetime.date(2026, 3, 1),
        )
    return employer, group


def a_cleaner(employer, group, *, hourly="40.00", start=ENGAGED):
    """R40 an hour, nine hours a day, five days a week: R1 800 a week, R360 a day."""
    n = next(_ids)
    body = f"8506155{n:03d}08"[:12]
    with tenant_context(employer.tenant_id):
        employee = Employee.objects.create(
            tenant=employer.tenant,
            employer=employer,
            first_name="Sipho",
            last_name=f"Dlamini{n}",
            date_of_birth=datetime.date(1985, 6, 15),
            mobile_number=f"+2782300{n:04d}",
            email=f"sipho{n}@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
    engagement = engage(employee, start_date=start, job_title="Cleaner")
    rate = Decimal(hourly)
    with tenant_context(employer.tenant_id):
        EmployeeRemuneration.objects.create(
            tenant=employer.tenant,
            employee=employee,
            engagement=engagement,
            pay_group=group,
            pay_basis="hourly",
            rate_amount=rate,
            hours_per_day=Decimal("9"),
            days_per_week=Decimal("5"),
            hours_per_week=Decimal("45"),
            derived_hourly_rate=rate,
            derived_daily_rate=rate * 9,
            derived_monthly_rate=(rate * 45 * Decimal("4.333333")).quantize(Decimal("0.000001")),
            effective_from=start,
        )
        EmployeeTaxProfile.objects.create(
            tenant=employer.tenant, employee=employee, tax_status="standard", effective_from=start
        )
        schedule = WorkSchedule.objects.create(
            tenant=employer.tenant,
            employee=employee,
            days_per_week=Decimal("5"),
            ordinary_hours_per_week=Decimal("45"),
            effective_from=start,
        )
        for cycle_day in range(7):
            working = cycle_day < 5
            WorkScheduleDay.objects.create(
                tenant=employer.tenant,
                work_schedule=schedule,
                cycle_day=cycle_day,
                is_working_day=working,
                ordinary_hours=Decimal("9") if working else Decimal("0"),
                start_time=datetime.time(7, 0) if working else None,
                end_time=datetime.time(17, 0) if working else None,
                unpaid_break_minutes=60 if working else 0,
            )
    return employee, engagement


def june(group, tax_year):
    with tenant_context(group.tenant_id):
        return PayPeriod.objects.create(
            tenant=group.tenant,
            pay_group=group,
            tax_year=tax_year,
            period_number=4,
            period_start=datetime.date(2026, 6, 1),
            period_end=datetime.date(2026, 6, 30),
            payment_date=datetime.date(2026, 6, 30),
            working_days_in_period=Decimal("22.000"),
        )


def worked_first_ten_days_of_june(employee):
    """1 to 12 June 2026: ten weekdays, nine ordinary hours each."""
    day = datetime.date(2026, 6, 1)
    with tenant_context(employee.tenant_id):
        while day <= LEAVING:
            if day.weekday() < 5:
                AttendanceDay.objects.create(
                    tenant=employee.tenant,
                    employee=employee,
                    work_date=day,
                    ordinary_hours=Decimal("9.00"),
                    days_worked_equivalent=Decimal("1.000"),
                )
            day += datetime.timedelta(days=1)


def annual_leave(employee, *, first_cycle_taken, second_cycle_accrued):
    """Cycle 1 (1 June 2025 - 31 May 2026): 15 days accrued, some taken.
    Cycle 2 (from 1 June 2026): what accrued by the last day.

    The cycles are written directly: the shipped reference data begins on 1 March
    2026, so ``ensure_cycles()`` cannot resolve a rule set for a cycle opening in
    June 2025. The ledger goes through ``post_transaction()`` as ever."""
    annual = LeaveType.objects.shared().get(code="ANNUAL")
    with transaction.atomic(), tenant_context(employee.tenant_id):
        engagement = EmployeeEngagement.objects.get(employee=employee)
        first, second = (
            LeaveCycle.objects.create(
                tenant=employee.tenant,
                employee=employee,
                engagement=engagement,
                leave_type=annual,
                cycle_number=number,
                cycle_start=start,
                cycle_end=end,
                unit="days",
                entitlement_quantity=Decimal("15.000"),
            )
            for number, start, end in (
                (1, ENGAGED, datetime.date(2026, 6, 1)),
                (2, datetime.date(2026, 6, 1), datetime.date(2027, 6, 1)),
            )
        )
    for cycle, kind, quantity, on in (
        (first, "accrual", Decimal("15.000"), datetime.date(2026, 5, 31)),
        (first, "taken", -first_cycle_taken, datetime.date(2026, 4, 1)),
        (second, "accrual", second_cycle_accrued, LEAVING),
    ):
        if quantity:
            with transaction.atomic(), tenant_context(employee.tenant_id):
                post_transaction(
                    employee=employee,
                    leave_cycle=cycle,
                    leave_type=annual,
                    transaction_type=kind,
                    quantity=quantity,
                    unit="days",
                    transaction_date=on,
                    reason="test ledger",
                )


def resigns_paid_in_lieu(engagement):
    return terminate(
        engagement,
        termination_date=LEAVING,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        notice_worked=False,
    )


def only_payslip(run):
    with tenant_context(run.tenant_id):
        (payslip,) = run.payslips.all()
        lines = list(payslip.lines.order_by("line_order"))
    return payslip, lines


def amounts(lines, code):
    return [line.amount for line in lines if line.component_code == code]


# ============================================================ the hand-computed leaver


def test_a_hand_computed_leavers_final_payslip(shipped):
    """An SD1 contract cleaner, R40 an hour × 45 a week, engaged 1 June 2025,
    resigns and is paid notice in lieu, last day Friday 12 June 2026.

    ORDINARY  BASIC        10 days × 9 h × R40,00                     = R3 600,00
    s38       NOTICE_PAY   4 weeks (SD1 23(1)(b), >4 weeks' service)
                           × R1 800,00 a week (40 × 45)               = R7 200,00
    s40(b)    LEAVE_PAYOUT 3 days due from cycle 1 (15 accrued − 12
                           taken) × R360,00 a day (1 800 ÷ 5)        = R1 080,00
    s40(c)    LEAVE_PAYOUT 1,250 days in cycle 2's ledger, which beats
                           s40(c)(i)'s floor of 10 worked ÷ 17 =
                           0,588 day                 1,25 × R360,00   =   R450,00
    SD1 3(3)  BONUS_PRO_RATA  5 full months (Jan-May) of the cycle ending
                           December: 4,333 × R1 800 × 5 ÷ 12          = R3 249,75
                                                         GROSS          R15 579,75

    PAYE (2027 tables). Annual payments (3605): 1 080 + 450 + 3 249,75 =
    R4 779,75. The rest is R10 800,00, earned in 12 of June's 30 days, so
    periods worked 0,4 and the annual equivalent 10 800 × 12 ÷ 0,4 = R324 000.
      tax on 324 000     = 44 118 + 26% × (324 000 − 245 100)     = 64 632,00
      less primary rebate 17 820                                   = 46 812,00
      this period's share  46 812 × 0,4 ÷ 12                       =  1 560,40
      tax on 328 779,75  = 44 118 + 26% × 83 679,75 = 65 874,735
      less 17 820 = 48 054,735; less 46 812 = the annual payments' tax  1 242,735
      PAYE 1 560,40 + 1 242,735 = 2 803,135                         →  R2 803,14
    UIF: every line is UIF remuneration (3601 and 3605); 1% × 15 579,75
      = 155,7975                                                    →    R155,80
    NET 15 579,75 − 2 803,14 − 155,80                                 = R12 620,81
    """
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    annual_leave(employee, first_cycle_taken=Decimal("12"), second_cycle_accrued=Decimal("1.25"))
    engagement = resigns_paid_in_lieu(engagement)

    payout = termination.prepare(engagement)
    assert (
        payout.notice_pay_amount,
        payout.leave_payout_amount,
        payout.bonus_pro_rata_amount,
        payout.severance_amount,
        payout.total_payout_gross,
    ) == (
        Decimal("7200.00"),
        Decimal("1530.00"),
        Decimal("3249.75"),
        Decimal("0.00"),
        Decimal("11979.75"),
    )
    assert (payout.notice_weeks_required, payout.bonus_months_worked) == (Decimal("4.00"), 5)
    termination.review(payout, reviewed_by=person)

    run = runs.calculate(runs.open_run(june(group, tax_year)))
    payslip, lines = only_payslip(run)

    assert payslip.is_termination_payslip
    assert [
        (line.component_code, line.amount) for line in lines if line.component_type == "earning"
    ] == [
        ("BASIC", Decimal("3600.00")),
        ("NOTICE_PAY", Decimal("7200.00")),
        ("LEAVE_PAYOUT", Decimal("1080.00")),
        ("LEAVE_PAYOUT", Decimal("450.00")),
        ("BONUS_PRO_RATA", Decimal("3249.75")),
    ]
    assert {line.source_code for line in lines if line.component_code == "LEAVE_PAYOUT"} == {"3605"}
    assert payslip.total_earnings == Decimal("15579.75")
    assert payslip.paye == Decimal("2803.14")
    assert payslip.uif_employee == Decimal("155.80")
    assert payslip.net_pay == Decimal("12620.81")

    with tenant_context(run.tenant_id):
        paye = PayrollCalculationTrace.objects.get(
            payslip=payslip, calculator_name="paye.employees_tax"
        )
    assert paye.inputs["annual_payment"] == "4779.75"
    assert paye.outputs["annual_equivalent"] == "324000.000000"


def test_finalising_processes_the_payout_and_records_the_bonus_paid(shipped):
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    annual_leave(employee, first_cycle_taken=Decimal("12"), second_cycle_accrued=Decimal("1.25"))
    engagement = resigns_paid_in_lieu(engagement)
    termination.review(termination.prepare(engagement), reviewed_by=person)
    run = runs.calculate(runs.open_run(june(group, tax_year)))
    for issue in validation.validate(run):
        if issue.severity == "error":
            validation.resolve_issue(issue, resolved_by=person, reason="test")

    runs.finalise(runs.approve(run, approved_by=person), finalised_by=person)

    with tenant_context(employer.tenant_id):
        payout = TerminationPayout.objects.get(engagement=engagement)
        cycle = AnnualBonusCycle.objects.get(employee=employee)
    assert (payout.status, payout.payroll_run_id) == ("processed", run.pk)
    assert (cycle.status, cycle.paid_amount, cycle.paid_in_payroll_run_id) == (
        "pro_rata_paid",
        Decimal("3249.75"),
        run.pk,
    )

    runs.reverse(run, reversed_by=person, reason="Wrong last day")

    with tenant_context(employer.tenant_id):
        payout.refresh_from_db()
        cycle.refresh_from_db()
    assert (payout.status, payout.payroll_run_id) == ("reviewed", None)
    assert (cycle.status, cycle.paid_in_payroll_run_id) == ("accruing", None)


# =================================================================== D-185


def test_an_overdrawn_balance_is_surfaced_and_never_netted_off(shipped):
    """D-185, where it bites. Cycle 1: 15 accrued, 18 taken — overdrawn by 3
    days, worth R1 080. The payout pays nothing for cycle 1 and does NOT subtract
    the R1 080: notice R7 200 + pro-rata R450 + bonus R3 249,75 = R10 899,75,
    exactly what it would be with a nil balance. A BLOCKING issue names the
    overdraw for a person to decide."""
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    annual_leave(employee, first_cycle_taken=Decimal("18"), second_cycle_accrued=Decimal("1.25"))
    engagement = resigns_paid_in_lieu(engagement)

    payout = termination.prepare(engagement)

    assert payout.leave_payout_amount == Decimal("450.00")
    assert payout.total_payout_gross == Decimal("10899.75")
    assert payout.calculation_detail["negative_leave_balances"] == [
        {"leave_type": "ANNUAL", "cycle_start": "2025-06-01", "balance": "-3.000", "unit": "days"}
    ]
    assert "NOT netted off" in " ".join(payout.calculation_detail["trace"]["warnings"])

    termination.review(payout, reviewed_by=person)
    run = runs.calculate(runs.open_run(june(group, tax_year)))
    payslip, lines = only_payslip(run)
    assert payslip.total_deductions == payslip.paye + payslip.uif_employee, "nothing else came off"

    (issue,) = [
        i for i in validation.validate(run) if i.issue_code == "leave_overdrawn_at_termination"
    ]
    assert issue.severity == "error"
    assert "overdrawn by 3.000 days" in issue.message
    assert "has NOT been netted off the final payment" in issue.message
    with pytest.raises(runs.ApprovalRefusedError, match="leave_overdrawn_at_termination"):
        runs.approve(run, approved_by=person)


# ============================================================== the workflow


def test_an_unprepared_payout_refuses_the_final_payslip(shipped):
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    resigns_paid_in_lieu(engagement)

    run = runs.calculate(runs.open_run(june(group, tax_year)))

    assert run.employee_count == 0
    (issue,) = [i for i in validation.validate(run) if i.issue_code.startswith("not_priced")]
    assert issue.issue_code == "not_priced:termination_payout_not_prepared"
    assert "no termination payout has been prepared" in issue.message


def test_an_unreviewed_payout_refuses_the_final_payslip(shipped):
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    termination.prepare(resigns_paid_in_lieu(engagement))

    run = runs.calculate(runs.open_run(june(group, tax_year)))

    (issue,) = [i for i in validation.validate(run) if i.issue_code.startswith("not_priced")]
    assert issue.issue_code == "not_priced:termination_payout_not_reviewed"


def test_a_payout_whose_rows_moved_after_review_refuses(shipped):
    """PROVE EVERY GUARD FAILS: reviewed at R1 800 a week, then the rate is
    corrected to R45 an hour. The payslip may not quietly pay a figure nobody
    reviewed."""
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    engagement = resigns_paid_in_lieu(engagement)
    termination.review(termination.prepare(engagement), reviewed_by=person)
    with tenant_context(employer.tenant_id):
        EmployeeRemuneration.objects.filter(employee=employee).update(
            rate_amount=Decimal("45.00"),
            derived_hourly_rate=Decimal("45.000000"),
            derived_daily_rate=Decimal("405.000000"),
        )

    run = runs.calculate(runs.open_run(june(group, tax_year)))

    (issue,) = [i for i in validation.validate(run) if i.issue_code.startswith("not_priced")]
    assert issue.issue_code == "not_priced:termination_payout_changed"
    assert "notice_pay_amount reviewed 7200.00, now 8100.00" in issue.message


def test_notice_not_declared_refuses_to_prepare(shipped):
    """s38 is the employer's election; a blank is not an election."""
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    engagement = terminate(
        engagement,
        termination_date=LEAVING,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    with pytest.raises(termination.PayoutRefusedError, match="record notice_worked") as refused:
        termination.prepare(engagement)
    assert refused.value.code == "notice_not_declared"


def test_notice_worked_pays_no_notice(shipped):
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    engagement = terminate(
        engagement,
        termination_date=LEAVING,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        notice_worked=True,
    )
    payout = termination.prepare(engagement)
    assert (payout.notice_pay_amount, payout.notice_worked) == (Decimal("0.00"), True)


def test_severance_is_priced_on_the_payout_and_refused_on_the_payslip(shipped):
    """A retrenchment after one completed year: s41(2) one week × R1 800. The
    payout shows it and flags the directive; the payslip refuses, because a
    severance benefit is taxed on a SARS directive nobody has captured (O-50)."""
    tax_year, person = shipped
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    worked_first_ten_days_of_june(employee)
    engagement = terminate(
        engagement,
        termination_date=LEAVING,
        reason_code=EmployeeEngagement.TerminationReason.RETRENCHMENT,
        notice_worked=True,
    )
    payout = termination.prepare(engagement)
    assert (payout.severance_amount, payout.severance_weeks, payout.severance_tax_treatment) == (
        Decimal("1800.00"),
        Decimal("1.00"),
        "directive_required",
    )
    termination.review(payout, reviewed_by=person)

    run = runs.calculate(runs.open_run(june(group, tax_year)))

    (issue,) = [i for i in validation.validate(run) if i.issue_code.startswith("not_priced")]
    assert issue.issue_code == "not_priced:severance_needs_directive"
    assert "O-50" in issue.message


def test_a_processed_payout_cannot_be_prepared_again(shipped):
    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    engagement = resigns_paid_in_lieu(engagement)
    payout = termination.prepare(engagement)
    with tenant_context(employer.tenant_id):
        run = PayrollRun.objects.create(
            tenant=employer.tenant,
            employer=employer,
            pay_period=june(group, TaxYear.objects.get(label="2026/2027")),
            engine_version="test",
        )
        TerminationPayout.objects.filter(pk=payout.pk).update(status="processed", payroll_run=run)

    with pytest.raises(termination.PayoutRefusedError, match="correcting it is a reversal"):
        termination.prepare(engagement)


# ============================================================== the table


def test_the_total_must_be_its_parts(shipped):
    """PROVE EVERY GUARD FAILS: the CHECK, watched refusing."""
    from django.db import IntegrityError

    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    payout = termination.prepare(resigns_paid_in_lieu(engagement))
    with (
        pytest.raises(IntegrityError, match="termination_payout_total_is_its_parts"),
        transaction.atomic(),
        tenant_context(employer.tenant_id),
    ):
        TerminationPayout.objects.filter(pk=payout.pk).update(total_payout_gross=Decimal("1.00"))


def test_processed_must_name_its_run(shipped):
    from django.db import IntegrityError

    employer, group = a_cleaning_employer()
    employee, engagement = a_cleaner(employer, group)
    payout = termination.prepare(resigns_paid_in_lieu(engagement))
    with (
        pytest.raises(IntegrityError, match="termination_payout_processed_names_its_run"),
        transaction.atomic(),
        tenant_context(employer.tenant_id),
    ):
        TerminationPayout.objects.filter(pk=payout.pk).update(status="processed")
