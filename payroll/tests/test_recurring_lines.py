"""P7 chunk 8c: recurring earnings and deductions on the regular payslip.

On the SHIPPED reference data, like ``test_assembly.py``, whose helpers these
reuse. Every expected figure is worked by hand in the docstring.

Recurring rows are created with ``objects.create()`` — bypassing ``clean()`` —
on purpose where a test is about a guard: a row that reached the table past
``clean()`` (an import, a shell, a line captured before the law moved) is
exactly what the payroll-time re-check exists for.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import AppUser, FileObject
from employees.models import EmployeeRecurringComponent
from employers.models import PayrollComponent
from payroll import assembly, runs, validation
from payroll.models import PayPeriod, PayrollCalculationTrace
from payroll.tests.test_assembly import (
    a_june_period,
    an_employee,
    an_employer,
    load_shipped_reference,
    only_payslip,
)
from statutory.models import ReferenceDataVersion, SarsSourceCode, Sector

pytestmark = pytest.mark.django_db

START = datetime.date(2025, 1, 6)


@pytest.fixture
def reference(db):
    """The shipped reference data, loaded exactly as ``test_assembly.py`` loads it."""
    return load_shipped_reference()


_keys = iter(range(1, 999))


def a_household(reference):
    employer, group = an_employer(Sector.Code.DOMESTIC, "Household")
    employee = an_employee(
        employer, group, basis="monthly", rate="5000.00", hourly="25.641026", daily="230.769231"
    )
    return employer, group, employee


def consent(tenant):
    with tenant_context(tenant.pk):
        return FileObject.objects.create(
            tenant=tenant,
            storage_key=f"consent/{next(_keys)}",
            original_filename="consent.pdf",
            content_type="application/pdf",
            size_bytes=10,
            checksum_sha256="0" * 64,
            scan_status=FileObject.ScanStatus.CLEAN,
        )


def system(code):
    return PayrollComponent.objects.shared().get(code=code)


def own_component(employer, code, *, kind, source=None, method="fixed"):
    """A component the employer added to the catalogue, flags copied from its
    SARS code the way ``PayrollComponent.clean()`` requires."""
    row = SarsSourceCode.objects.get(code=source) if source else None
    with tenant_context(employer.tenant_id):
        return PayrollComponent.objects.create(
            tenant=employer.tenant,
            code=code,
            name=code.title(),
            component_type=kind,
            calculation_method=method,
            sars_source_code=row,
            is_taxable=row.is_taxable if row else False,
            is_uif_base=row.is_uif_remuneration if row else False,
            is_sdl_base=row.is_sdl_remuneration if row else False,
            is_coida_base=row.is_coida_remuneration if row else False,
        )


def a_line(employee, component, *, with_consent=True, **fields):
    with tenant_context(employee.tenant_id):
        return EmployeeRecurringComponent.objects.create(
            tenant=employee.tenant,
            employee=employee,
            payroll_component=component,
            effective_from=fields.pop("effective_from", START),
            written_consent_file=consent(employee.tenant) if with_consent else None,
            **fields,
        )


def calculated(period):
    return runs.calculate(runs.open_run(period))


def refusal(run, employee):
    (issue,) = [
        i
        for i in validation.validate(run)
        if i.employee_id == employee.pk and i.issue_code.startswith("not_priced")
    ]
    return issue


# ================================================================ earnings


def test_a_taxable_allowance_joins_every_base_on_its_flags(reference):
    """R5 000 salary and a R600 transport allowance on SARS code 3713 (taxable,
    UIF and SDL remuneration).

    Gross R5 600. PAYE: 5 600 × 12 = R67 200 — under the R99 000 threshold, nil.
    UIF: 1% of R5 600 = R56,00 each side. Net 5 600 − 56 = R5 544,00.
    """
    employer, group, employee = a_household(reference)
    transport = own_component(employer, "TRANSPORT", kind="earning", source="3713")
    line = a_line(employee, transport, amount=Decimal("600.00"), with_consent=False)

    run = calculated(a_june_period(group, reference))
    payslip, lines = only_payslip(run)

    assert lines["TRANSPORT"].amount == Decimal("600.00")
    assert lines["TRANSPORT"].source_code == "3713"
    assert lines["TRANSPORT"].recurring_component_id == line.pk
    assert (payslip.total_earnings, payslip.taxable_remuneration) == (
        Decimal("5600.00"),
        Decimal("5600.00"),
    )
    assert payslip.uif_employee == Decimal("56.00")
    assert payslip.net_pay == Decimal("5544.00")


def test_a_non_taxable_allowance_is_paid_and_stays_out_of_the_bases(reference):
    """R400 on SARS code 3714 — non-taxable, not UIF remuneration.

    Gross R5 400, taxable R5 000, UIF 1% of R5 000 = R50,00. Net R5 350,00.
    """
    employer, group, employee = a_household(reference)
    tools = own_component(employer, "TOOLS", kind="earning", source="3714")
    a_line(employee, tools, amount=Decimal("400.00"), with_consent=False)

    payslip, _ = only_payslip(calculated(a_june_period(group, reference)))

    assert payslip.total_earnings == Decimal("5400.00")
    assert payslip.taxable_remuneration == Decimal("5000.00")
    assert payslip.uif_remuneration == Decimal("5000.00")
    assert payslip.net_pay == Decimal("5350.00")


def test_a_fringe_benefit_is_not_paid_out_as_cash(reference):
    """PROVE EVERY GUARD FAILS: a 3801 fringe benefit is taxable but not cash;
    as a recurring earning it would land in net pay."""
    employer, group, employee = a_household(reference)
    benefit = own_component(employer, "CAR", kind="earning", source="3801")
    a_line(employee, benefit, amount=Decimal("900.00"), with_consent=False)

    run = calculated(a_june_period(group, reference))

    assert run.employee_count == 0
    issue = refusal(run, employee)
    assert issue.issue_code == "not_priced:fringe_benefit_not_built"
    assert "CAR is a fringe benefit (SARS code 3801)" in issue.message


# ============================================================== deductions


def test_accommodation_at_the_sd7_ceiling(reference):
    """SD7 permits 10% of the wage for accommodation.

    10% of R5 000 = R500,00. UIF R50,00. Deductions R550,00, net R4 450,00, and
    the trace records the rule set row the ceiling came from.
    """
    employer, group, employee = a_household(reference)
    a_line(employee, system("ACCOM_DED"), percentage_of_basic=Decimal("10.0000"))

    run = calculated(a_june_period(group, reference))
    payslip, lines = only_payslip(run)

    assert lines["ACCOM_DED"].amount == Decimal("500.00")
    assert lines["ACCOM_DED"].component_type == "deduction"
    assert lines["ACCOM_DED"].calculation_note == "10.0000% of basic 5000.000000"
    assert (payslip.total_deductions, payslip.net_pay) == (Decimal("550.00"), Decimal("4450.00"))
    assert run.total_other_deductions == Decimal("500.00")
    with tenant_context(run.tenant_id):
        trace = PayrollCalculationTrace.objects.get(
            payslip=payslip, calculator_name="recurring.recurring_lines"
        )
    assert [table for table, _ in trace.reference_rows_used] == ["working_time_rule_set"]


def test_accommodation_above_the_ceiling_in_force_refuses(reference):
    """PROVE EVERY GUARD FAILS: 12% reached the table past clean() — the
    re-check at the period refuses it, naming both figures."""
    employer, group, employee = a_household(reference)
    a_line(employee, system("ACCOM_DED"), percentage_of_basic=Decimal("12.0000"))

    run = calculated(a_june_period(group, reference))

    issue = refusal(run, employee)
    assert issue.issue_code == "not_priced:recurring_refused"
    assert "ACCOM_DED: 600.00 is more than the 10.00% of the wage" in issue.message


def test_a_deduction_without_written_consent_refuses(reference):
    """PROVE EVERY GUARD FAILS: BCEA s34(1)."""
    employer, group, employee = a_household(reference)
    a_line(employee, system("ADVANCE_DED"), amount=Decimal("100.00"), with_consent=False)

    run = calculated(a_june_period(group, reference))

    issue = refusal(run, employee)
    assert issue.issue_code == "not_priced:deduction_without_consent"
    assert "BCEA s34(1)" in issue.message


def test_a_deleted_consent_file_is_no_consent(reference):
    employer, group, employee = a_household(reference)
    line = a_line(employee, system("ADVANCE_DED"), amount=Decimal("100.00"))
    with tenant_context(employer.tenant_id):
        FileObject.objects.filter(pk=line.written_consent_file_id).update(
            deleted_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
        )

    issue = refusal(calculated(a_june_period(group, reference)), employee)
    assert issue.issue_code == "not_priced:deduction_without_consent"


def test_a_deduction_that_changes_the_tax_refuses(reference):
    """PROVE EVERY GUARD FAILS: a medical scheme contribution (4005) earns a
    s6A credit and lands on the IRP5; deducting it as a plain deduction would
    silently get the tax wrong."""
    employer, group, employee = a_household(reference)
    medical = own_component(employer, "MEDICAL", kind="deduction", source="4005")
    a_line(employee, medical, amount=Decimal("800.00"))

    issue = refusal(calculated(a_june_period(group, reference)), employee)
    assert issue.issue_code == "not_priced:deduction_affects_tax"
    assert "SARS code 4005" in issue.message


def test_an_employer_contribution_line_refuses(reference):
    employer, group, employee = a_household(reference)
    pension = own_component(employer, "PENSION_ER", kind="employer_contribution")
    a_line(employee, pension, amount=Decimal("250.00"), with_consent=False)

    issue = refusal(calculated(a_june_period(group, reference)), employee)
    assert issue.issue_code == "not_priced:recurring_type_not_built"


def test_a_formula_line_refuses(reference):
    employer, group, employee = a_household(reference)
    odd = own_component(employer, "ODD", kind="earning", method="formula")
    a_line(employee, odd, amount=Decimal("1.00"), with_consent=False)

    issue = refusal(calculated(a_june_period(group, reference)), employee)
    assert issue.issue_code == "not_priced:recurring_method_not_built"


def test_deductions_that_take_net_below_zero_refuse_the_payslip(reference):
    """PROVE EVERY GUARD FAILS (D-301): R5 000 less UIF R50 less a R6 000
    instalment is −R1 050. Never stored; the employee is named instead."""
    employer, group, employee = a_household(reference)
    a_line(employee, system("ADVANCE_DED"), amount=Decimal("6000.00"))

    run = calculated(a_june_period(group, reference))

    assert run.employee_count == 0
    issue = refusal(run, employee)
    assert issue.issue_code == "not_priced:negative_net"
    assert (
        "Deductions of 6050.00 exceed earnings of 5000.00 by 1050.00 "
        "(PAYE 0.00, UIF_EE 50.00, ADVANCE_DED 6000.00)" in issue.message
    )
    assert "no deduction is quietly held back" in issue.message


def test_a_line_not_in_force_or_inactive_is_not_priced(reference):
    employer, group, employee = a_household(reference)
    advance = system("ADVANCE_DED")
    a_line(employee, advance, amount=Decimal("100.00"), is_active=False)
    a_line(
        employee,
        advance,
        amount=Decimal("200.00"),
        effective_to=datetime.date(2026, 6, 30),
    )
    a_line(employee, advance, amount=Decimal("300.00"), effective_from=datetime.date(2026, 7, 1))

    _, lines = only_payslip(calculated(a_june_period(group, reference)))
    assert "ADVANCE_DED" not in lines


# ============================================================ the loan


def a_july_period(group, tax_year):
    with tenant_context(group.tenant_id):
        return PayPeriod.objects.create(
            tenant=group.tenant,
            pay_group=group,
            tax_year=tax_year,
            period_number=5,
            period_start=datetime.date(2026, 7, 1),
            period_end=datetime.date(2026, 7, 31),
            payment_date=datetime.date(2026, 7, 31),
            working_days_in_period=Decimal("23.000"),
        )


def finalised(run, person):
    for issue in validation.validate(run):
        if issue.severity == "error":
            validation.resolve_issue(issue, resolved_by=person, reason="test")
    runs.finalise(runs.approve(run, approved_by=person), finalised_by=person)


def test_a_loan_is_paid_down_by_finalised_payslips_only(reference):
    """D-303: R600 advanced, R400 a month.

    June deducts R400. July, calculated while June is still a DRAFT, deducts
    R400 too — a draft recovers nothing. Once June is finalised, July's
    recalculation deducts the R200 still owed. ``balance_outstanding`` still
    reads R600: it is the principal, and nothing writes it.
    """
    person = AppUser.objects.create_user(email="verifier@example.com", password="x" * 16)
    for version in ReferenceDataVersion.objects.all():
        version.verified_at = datetime.datetime(2026, 9, 24, tzinfo=datetime.UTC)
        version.verified_by_user = person
        version.golden_tests_passed = True
        version.data_current_through = datetime.date(2027, 2, 28)
        version.save()

    employer, group, employee = a_household(reference)
    loan = a_line(
        employee,
        system("ADVANCE_DED"),
        amount=Decimal("400.00"),
        balance_outstanding=Decimal("600.00"),
    )

    june = calculated(a_june_period(group, reference))
    assert only_payslip(june)[1]["ADVANCE_DED"].amount == Decimal("400.00")
    july_period = a_july_period(group, reference)
    july = calculated(july_period)
    assert only_payslip(july)[1]["ADVANCE_DED"].amount == Decimal("400.00")
    with tenant_context(employer.tenant_id):
        assert assembly.owed_on(loan) == Decimal("600.00")

    finalised(june, person)
    july = runs.calculate(july)

    line = only_payslip(july)[1]["ADVANCE_DED"]
    assert line.amount == Decimal("200.00")
    assert line.calculation_note == "final instalment: 200.00 was owed"
    with tenant_context(employer.tenant_id):
        loan.refresh_from_db()
        assert assembly.owed_on(loan) == Decimal("200.00")
    assert loan.balance_outstanding == Decimal("600.00")


def test_a_loan_paid_off_produces_no_line(reference):
    employer, group, employee = a_household(reference)
    a_line(
        employee,
        system("ADVANCE_DED"),
        amount=Decimal("400.00"),
        balance_outstanding=Decimal("0.00"),
    )

    _, lines = only_payslip(calculated(a_june_period(group, reference)))
    assert "ADVANCE_DED" not in lines


def test_the_lines_own_ceiling_holds_the_deduction(reference):
    """R1 000 an instalment, capped at 10% of basic by the line itself → R500."""
    employer, group, employee = a_household(reference)
    a_line(
        employee,
        system("ADVANCE_DED"),
        amount=Decimal("1000.00"),
        total_deduction_cap_pct=Decimal("10.00"),
    )

    payslip, lines = only_payslip(calculated(a_june_period(group, reference)))
    assert lines["ADVANCE_DED"].amount == Decimal("500.00")
    assert lines["ADVANCE_DED"].calculation_note == "held to the line's own 10.00% ceiling"
    assert payslip.net_pay == Decimal("4450.00")


def test_reversing_a_run_puts_a_loan_instalment_back(reference):
    """D-303 through invariant 4: June recovers R400 of R600; reversing June
    mirrors that line NEGATIVELY and against the same recurring row, so what is
    owed returns to R600. A mirror that dropped the link would leave R200 owed
    for money the employee has been given back."""
    person = AppUser.objects.create_user(email="verifier@example.com", password="x" * 16)
    for version in ReferenceDataVersion.objects.all():
        version.verified_at = datetime.datetime(2026, 9, 24, tzinfo=datetime.UTC)
        version.verified_by_user = person
        version.golden_tests_passed = True
        version.data_current_through = datetime.date(2027, 2, 28)
        version.save()
    employer, group, employee = a_household(reference)
    loan = a_line(
        employee,
        system("ADVANCE_DED"),
        amount=Decimal("400.00"),
        balance_outstanding=Decimal("600.00"),
    )
    june = calculated(a_june_period(group, reference))
    finalised(june, person)
    with tenant_context(employer.tenant_id):
        assert assembly.owed_on(loan) == Decimal("200.00")

    runs.reverse(june, reversed_by=person, reason="Wrong instalment captured")

    with tenant_context(employer.tenant_id):
        assert assembly.owed_on(loan) == Decimal("600.00")
