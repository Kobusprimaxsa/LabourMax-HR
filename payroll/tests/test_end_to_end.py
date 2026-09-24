"""P7's "Done when", proven end to end on the SHIPPED reference data (task 5).

One tenant, one month — June 2026 — and every kind of employee the brief
names. An employer has exactly ONE sector, so domestic (SD7) and contract
cleaning Area B (BCCCI) cannot share an employer; the tenant runs two employing
entities, which a tenant may (D-140), paid for the same month:

    Household (SD7, monthly)       d1  R15 000 with two medical scheme members
                                   d2  R5 000 with a 10% accommodation deduction
    Cleaners (BCCCI Area B, hourly) c1  took three days' annual leave
                                   c2  left on 12 June, notice paid in lieu
                                   c3  one day not captured - blocks approval

draft → calculate → calculated → approved → finalised, then:

* every calculated payslip reproduces from its own traces ALONE
  (``payroll/replay.py``, D-313) — the phase's definition of done;
* the year-to-date rebuild matches a ground truth computed here, from the
  payslips and lines, without reading the cache or ``payroll/ytd.py``;
* an uncaptured attendance-driven day blocks approval;
* a closed period refuses a second run;
* reversal produces a mirrored payslip and leaves the original untouched.
"""

from __future__ import annotations

import collections
import datetime
from decimal import Decimal

import pytest
from django.db import transaction

from attendance.models import AttendanceDay
from core.managers import tenant_context
from core.models import AppUser, FileObject, Tenant, TenantMembership
from employees.engagements import terminate
from employees.models import EmployeeEngagement, EmployeeRecurringComponent, EmployeeTaxProfile
from employers.models import Employer, PayGroup, PayrollComponent
from leave.applications import submit_application
from leave.authorisation import approve as approve_leave
from leave.cycles import current_cycle, ensure_cycles
from leave.ledger import post_transaction
from leave.models import LeaveType
from payroll import lifecycle, replay, runs, termination, validation
from payroll.models import (
    PayPeriod,
    PayrollCalculationTrace,
    PayrollRun,
    Payslip,
    YtdAccumulator,
)
from payroll.tests.test_termination_payslip import a_cleaner, load_shipped
from statutory.models import Sector, SectorArea

pytestmark = pytest.mark.django_db

JUNE = (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30))
YOUTH_DAY = datetime.date(2026, 6, 16)
APRIL = datetime.date(2026, 4, 1)
_files = iter(range(1, 999))


# ------------------------------------------------------------------- the world


def an_employing_entity(tenant, name, sector_code, *, area=None, frequency):
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant,
            trading_name=name,
            sector=Sector.objects.get(code=sector_code),
            sector_area=area,
        )
        group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name=name,
            pay_frequency=frequency,
            period_end_rule=PayGroup.PeriodEndRule.CALENDAR_MONTH_END,
            first_period_start=datetime.date(2026, 3, 1),
        )
    return employer, group


def june(group, tax_year):
    with tenant_context(group.tenant_id):
        return PayPeriod.objects.create(
            tenant=group.tenant,
            pay_group=group,
            tax_year=tax_year,
            period_number=4,
            period_start=JUNE[0],
            period_end=JUNE[1],
            payment_date=JUNE[1],
            working_days_in_period=Decimal("22.000"),
        )


def monthly(employer, group, rate):
    from payroll.tests.test_assembly import an_employee

    return an_employee(
        employer,
        group,
        basis="monthly",
        rate=rate,
        hourly=str((Decimal(rate) / Decimal("173.333333")).quantize(Decimal("0.000001"))),
        daily=str((Decimal(rate) / Decimal("21.666667")).quantize(Decimal("0.000001"))),
    )


def attendance(employee, days, *, day_type="ordinary"):
    with tenant_context(employee.tenant_id):
        for day in days:
            worked = day_type == "ordinary"
            AttendanceDay.objects.create(
                tenant=employee.tenant,
                employee=employee,
                work_date=day,
                day_type=day_type,
                ordinary_hours=Decimal("9.00") if worked else Decimal("0"),
                days_worked_equivalent=Decimal("1.000") if worked else Decimal("0"),
            )


def weekdays(first, last, *, skip=()):
    day, out = first, []
    while day <= last:
        if day.weekday() < 5 and day not in skip:
            out.append(day)
        day += datetime.timedelta(days=1)
    return out


def consent(tenant):
    with tenant_context(tenant.pk):
        return FileObject.objects.create(
            tenant=tenant,
            storage_key=f"consent/e2e-{next(_files)}",
            original_filename="consent.pdf",
            content_type="application/pdf",
            size_bytes=10,
            checksum_sha256="0" * 64,
            scan_status=FileObject.ScanStatus.CLEAN,
        )


@pytest.fixture
def world(db):
    tax_year, person = load_shipped()
    tenant = Tenant.objects.create(trading_name="One tenant")
    admin = AppUser.objects.create_user(email="admin@example.com", password="x" * 16)
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(tenant=tenant, user=person, role="owner")
        TenantMembership.objects.create(tenant=tenant, user=admin, role="admin")

    household, monthly_group = an_employing_entity(
        tenant, "Household", Sector.Code.DOMESTIC, frequency=PayGroup.PayFrequency.MONTHLY
    )
    area_b = SectorArea.objects.get(sector__code=Sector.Code.CONTRACT_CLEANING, code="AREA_B")
    cleaners, hourly_group = an_employing_entity(
        tenant,
        "Cleaners",
        Sector.Code.CONTRACT_CLEANING,
        area=area_b,
        frequency=PayGroup.PayFrequency.HOURLY,
    )

    d1 = monthly(household, monthly_group, "15000.00")
    with tenant_context(tenant.pk):
        EmployeeTaxProfile.objects.filter(employee=d1).update(medical_scheme_members=2)
    d2 = monthly(household, monthly_group, "5000.00")
    with tenant_context(tenant.pk):
        EmployeeRecurringComponent.objects.create(
            tenant=tenant,
            employee=d2,
            payroll_component=PayrollComponent.objects.shared().get(code="ACCOM_DED"),
            percentage_of_basic=Decimal("10.0000"),
            effective_from=datetime.date(2025, 1, 6),
            written_consent_file=consent(tenant),
        )

    c1, _ = a_cleaner(cleaners, hourly_group, start=APRIL)
    c2, c2_engagement = a_cleaner(cleaners, hourly_group, start=APRIL)
    c3, _ = a_cleaner(cleaners, hourly_group, start=APRIL)

    # c1: three days' annual leave, 8 to 10 June, approved through the real path.
    leave_days = [datetime.date(2026, 6, 8), datetime.date(2026, 6, 9), datetime.date(2026, 6, 10)]
    annual = LeaveType.objects.shared().get(code="ANNUAL")
    with transaction.atomic(), tenant_context(tenant.pk):
        ensure_cycles(c1, annual, horizon=JUNE[0])
        post_transaction(
            employee=c1,
            leave_cycle=current_cycle(c1, annual, JUNE[0]),
            leave_type=annual,
            transaction_type="accrual",
            quantity=Decimal("3.000"),
            unit="days",
            transaction_date=datetime.date(2026, 5, 31),
            reason="accrued to 31 May",
        )
    application = submit_application(
        c1, leave_type=annual, start_date=leave_days[0], end_date=leave_days[-1]
    )
    approve_leave(application, decided_by=admin)
    attendance(c1, weekdays(*JUNE, skip={YOUTH_DAY, *leave_days}))
    attendance(c1, [YOUTH_DAY], day_type="public_holiday")

    # c2: left on Friday 12 June, notice paid in lieu, payout prepared and reviewed.
    attendance(c2, weekdays(JUNE[0], datetime.date(2026, 6, 12)))
    c2_engagement = terminate(
        c2_engagement,
        termination_date=datetime.date(2026, 6, 12),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        notice_worked=False,
    )
    termination.review(termination.prepare(c2_engagement), reviewed_by=person)

    # c3: 15 June is not captured.
    attendance(c3, weekdays(*JUNE, skip={YOUTH_DAY, datetime.date(2026, 6, 15)}))
    attendance(c3, [YOUTH_DAY], day_type="public_holiday")

    return {
        "tax_year": tax_year,
        "person": person,
        "tenant": tenant,
        "periods": (june(monthly_group, tax_year), june(hourly_group, tax_year)),
        "d1": d1,
        "d2": d2,
        "c1": c1,
        "c2": c2,
        "c3": c3,
    }


def blocking(run):
    return {i.issue_code for i in validation.validate(run) if i.severity == "error"}


def payslips_of(run):
    with tenant_context(run.tenant_id):
        return {p.employee_id: p for p in run.payslips.select_related("employee")}


def lines_of(payslip):
    with tenant_context(payslip.tenant_id):
        return list(payslip.lines.order_by("line_order"))


# ================================================================== the proof


def test_one_month_end_to_end(world):
    person = world["person"]
    household_period, cleaners_period = world["periods"]

    household = runs.calculate(runs.open_run(household_period))
    cleaners = runs.calculate(runs.open_run(cleaners_period))
    assert (household.status, cleaners.status) == ("calculated", "calculated")
    assert (household.employee_count, cleaners.employee_count) == (2, 3)

    # ---------------------------------------- an uncaptured day blocks approval
    assert "attendance_incomplete" in blocking(cleaners)
    with pytest.raises(runs.ApprovalRefusedError, match="attendance_incomplete"):
        runs.approve(cleaners, approved_by=person)
    attendance(world["c3"], [datetime.date(2026, 6, 15)])
    cleaners = runs.calculate(cleaners)
    assert "attendance_incomplete" not in blocking(cleaners)

    # ------------------------------------------------------ what each payslip is
    paid = {**payslips_of(household), **payslips_of(cleaners)}
    d1, d2, c1, c2, c3 = (paid[world[k].pk] for k in ("d1", "d2", "c1", "c2", "c3"))
    with tenant_context(world["tenant"].pk):
        d1_paye = PayrollCalculationTrace.objects.get(
            payslip=d1, calculator_name="paye.employees_tax"
        )
    assert d1_paye.inputs["medical_scheme_members"] == "2"
    assert Decimal(d1_paye.outputs["medical_credit_applied"]) > 0
    assert [(x.component_code, x.amount) for x in lines_of(d2) if x.component_type == "deduction"][
        -1
    ] == ("ACCOM_DED", Decimal("500.00"))
    leave = [x for x in lines_of(c1) if x.component_code == "LEAVE_PAY"]
    assert [(x.units, x.unit_type) for x in leave] == [(Decimal("3.0000"), "days")]
    assert c2.is_termination_payslip
    assert "NOTICE_PAY" in {x.component_code for x in lines_of(c2)}
    assert not c3.is_termination_payslip

    # --------------------------------- every payslip reproduces from its traces
    for payslip in paid.values():
        assert replay.reproduce(payslip) == [], payslip.employee

    # ----------------------------------------------------------- to finalised
    for run in (household, cleaners):
        for issue in validation.validate(run):
            if issue.severity == "error":
                validation.resolve_issue(issue, resolved_by=person, reason="end to end")
        runs.finalise(runs.approve(run, approved_by=person), finalised_by=person)

    # ---------------------- the year to date, against a truth computed here
    for key in ("d1", "d2", "c1", "c2", "c3"):
        employee = world[key]
        with tenant_context(world["tenant"].pk):
            final = list(Payslip.objects.filter(employee=employee, is_finalised=True))
            row = YtdAccumulator.objects.get(employee=employee, tax_year=world["tax_year"])
            by_code = collections.defaultdict(Decimal)
            for payslip in final:
                for line in payslip.lines.all():
                    if line.source_code:
                        by_code[line.source_code] += line.amount
        assert row.ytd_gross == sum((p.gross_remuneration for p in final), Decimal(0))
        assert row.ytd_taxable == sum((p.taxable_remuneration for p in final), Decimal(0))
        assert row.ytd_paye == sum((p.paye for p in final), Decimal(0))
        assert row.ytd_uif_employee == sum((p.uif_employee for p in final), Decimal(0))
        assert row.ytd_uif_employer == sum((p.uif_employer for p in final), Decimal(0))
        assert row.periods_processed == 1
        assert {code: Decimal(v) for code, v in row.ytd_by_source_code.items()} == dict(by_code)

    # -------------------------------------- a closed period refuses a second run
    with tenant_context(world["tenant"].pk):
        household_period.refresh_from_db()
    assert household_period.status == "closed"
    with pytest.raises(lifecycle.PeriodError, match="is closed"):
        runs.open_run(household_period)

    # ------------------------------- reversal mirrors and leaves the original be
    before = {
        p.pk: (
            p.net_pay,
            p.paye,
            p.is_finalised,
            [(x.component_code, x.amount) for x in lines_of(p)],
        )
        for p in payslips_of(household).values()
    }
    reversal = runs.reverse(household, reversed_by=person, reason="End-to-end reversal")
    with tenant_context(world["tenant"].pk):
        household.refresh_from_db()
        mirrors = list(Payslip.objects.filter(payroll_run=reversal).order_by("pk"))
    assert household.status == PayrollRun.Status.REVERSED
    after = {
        p.pk: (
            p.net_pay,
            p.paye,
            p.is_finalised,
            [(x.component_code, x.amount) for x in lines_of(p)],
        )
        for p in payslips_of(household).values()
    }
    assert after == before, "the original payslips are untouched"
    assert len(mirrors) == 2
    for mirror in mirrors:
        original = before[mirror.reverses_payslip_id]
        assert mirror.net_pay == -original[0]
        assert [(x.component_code, x.amount) for x in lines_of(mirror)] == [
            (code, -amount) for code, amount in original[3]
        ]
    with tenant_context(world["tenant"].pk):
        household_period.refresh_from_db()
        d1_ytd = YtdAccumulator.objects.filter(employee=world["d1"]).first()
    assert (household_period.status, household_period.reopened_count) == ("reopened", 1)
    assert d1_ytd is None or d1_ytd.ytd_gross == 0, "the reversal nets the year back to nil"
