"""Leave entitlements, recurring payslip lines, and file notes.

Three tables that finish the employee master file, and two of them carry a way of
being wrong that is invisible on screen:

- an entitlement captured as *additive* when the employer meant *total*, which
  turns 21 days into 36 and triples the leave liability without a single error,
- a deduction with no written consent behind it, which is unlawful under BCEA
  s34(1) and looks exactly like a lawful one on the payslip.

``leave_type`` is tested here too rather than in P6, because it arrives with these
tables (D-127) and nothing in P6 exists yet to test it against.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction

from core.managers import platform_context, tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import (
    Employee,
    EmployeeLeaveEntitlement,
    EmployeeNote,
    EmployeeRecurringComponent,
)
from employees.remuneration import MONTHLY_FACTOR_PARAMETER
from employers.models import Employer, PayrollComponent
from leave.models import LeaveType
from statutory.models import SarsSourceCode, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)
LATER = datetime.date(2026, 7, 1)


def make_id(sequence="5009"):
    body = f"900101{sequence}08"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def parameters(db):
    for code, value, unit in (
        (MINIMUM_AGE_PARAMETER, "15.000000", StatutoryParameter.Unit.YEARS),
        (MONTHLY_FACTOR_PARAMETER, "4.333333", StatutoryParameter.Unit.RATIO),
    ):
        StatutoryParameter.objects.create(
            parameter_code=code,
            value_numeric=Decimal(value),
            unit=unit,
            effective_from=datetime.date(1997, 12, 1),
            source_reference="Basic Conditions of Employment Act 75 of 1997",
        )


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Subscriber")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)


@pytest.fixture
def employee(db, tenant, employer, parameters):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=BORN,
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=make_id(),
        )
    engage(person, start_date=START, job_title="Domestic worker")
    return person


@pytest.fixture
def annual(db):
    """A shared leave type, as the platform stocks it.

    ``is_system`` because a shared row IS a system row — the CHECK pair added with
    the column says so, for the same reason payroll_component's does (D-93).
    """
    with platform_context():
        return LeaveType.objects.create(
            code=LeaveType.Code.ANNUAL,
            name="Annual leave",
            payable_on_termination=True,
            is_system=True,
        )


@pytest.fixture
def source_code(db):
    return SarsSourceCode.objects.create(
        code="3601",
        description="Income (Subject to PAYE)",
        code_group=SarsSourceCode.Group.INCOME,
        is_taxable=True,
        is_uif_remuneration=True,
        is_sdl_remuneration=True,
        is_coida_remuneration=True,
        source_reference="SARS Guide for Codes Applicable to Employees Tax Certificates",
    )


def make_component(tenant, code, component_type, *, method=None, source_code=None):
    with tenant_context(tenant.pk):
        return PayrollComponent.objects.create(
            tenant=tenant,
            code=code,
            name=code.replace("_", " ").title(),
            component_type=component_type,
            calculation_method=method or PayrollComponent.CalculationMethod.FIXED,
            sars_source_code=source_code,
        )


# ------------------------------------------------------------------ leave_type


def test_a_sub_type_draws_on_its_parents_balance(annual):
    """Unauthorised annual leave consumes the annual balance, it does not sit apart.

    If it kept its own balance the employer would record the absence and the
    employee would keep the day, which is the opposite of what recording it is for.
    """
    with platform_context():
        unauthorised = LeaveType.objects.create(
            code=LeaveType.Code.ANNUAL_UNAUTHORISED,
            name="Annual leave — unauthorised",
            parent_leave_type=annual,
            balance_source=LeaveType.BalanceSource.PARENT,
            accrues=False,
            is_paid=False,
            is_system=True,
        )

    assert unauthorised.parent_leave_type_id == annual.pk
    assert unauthorised.balance_source == LeaveType.BalanceSource.PARENT


def test_drawing_on_a_parent_balance_requires_a_parent(db):
    with platform_context(), pytest.raises(IntegrityError), transaction.atomic():
        LeaveType.objects.create(
            code="ORPHAN",
            name="Orphan",
            balance_source=LeaveType.BalanceSource.PARENT,
        )


def test_sub_types_are_one_level_deep(annual):
    """A chain means resolving a balance walks an unknown number of rows."""
    with platform_context():
        middle = LeaveType.objects.create(
            code=LeaveType.Code.ANNUAL_UNAUTHORISED,
            name="Unauthorised",
            parent_leave_type=annual,
            balance_source=LeaveType.BalanceSource.PARENT,
            accrues=False,
            is_system=True,
        )
        deeper = LeaveType(
            code="DEEPER",
            name="Deeper",
            parent_leave_type=middle,
            balance_source=LeaveType.BalanceSource.PARENT,
            accrues=False,
        )
        with pytest.raises(ValidationError) as raised:
            deeper.full_clean()

    assert "one level deep" in str(raised.value)


def test_a_type_with_no_balance_cannot_accrue(db):
    with platform_context():
        unpaid = LeaveType(
            code=LeaveType.Code.UNPAID,
            name="Unpaid leave",
            balance_source=LeaveType.BalanceSource.NONE,
            accrues=True,
            is_paid=False,
        )
        with pytest.raises(ValidationError) as raised:
            unpaid.full_clean()

    assert "accrue" in str(raised.value)


def test_unpaid_leave_cannot_be_paid_out_on_termination(db):
    with platform_context():
        unpaid = LeaveType(
            code=LeaveType.Code.UNPAID,
            name="Unpaid leave",
            is_paid=False,
            accrues=False,
            balance_source=LeaveType.BalanceSource.NONE,
            payable_on_termination=True,
        )
        with pytest.raises(ValidationError) as raised:
            unpaid.full_clean()

    assert "no rate to pay it at" in str(raised.value)


def test_the_platform_cannot_stock_one_code_twice(annual):
    """NULL = NULL is unknown, so the unique is over Coalesce(tenant_id, 0)."""
    with platform_context(), pytest.raises(IntegrityError), transaction.atomic():
        LeaveType.objects.create(code=LeaveType.Code.ANNUAL, name="Annual leave again")


def test_an_employer_may_add_its_own_type_beside_the_shared_one(tenant, annual):
    """Same code, different scope. The employer's row is its own, not a clash."""
    with tenant_context(tenant.pk):
        mine = LeaveType.objects.create(
            tenant=tenant, code=LeaveType.Code.ANNUAL, name="Our annual leave"
        )
        assert mine.tenant_id == tenant.pk
        assert LeaveType.objects.filter(code=LeaveType.Code.ANNUAL).count() == 2


def test_a_tenant_cannot_delete_a_shared_leave_type(tenant, annual):
    """DELETE is checked against USING only, so the trigger is the layer that holds.

    Without it, one employer deleting a statutory type removes it from every other
    employer's leave screen (D-93).
    """
    with tenant_context(tenant.pk), pytest.raises(DatabaseError), transaction.atomic():
        LeaveType.all_tenants.filter(pk=annual.pk).delete()


# --------------------------------------------------------- leave entitlements


def make_entitlement(employee, leave_type, **overrides):
    values = {"effective_from": START, **overrides}
    with tenant_context(employee.tenant_id):
        return EmployeeLeaveEntitlement.objects.create(
            tenant=employee.tenant, employee=employee, leave_type=leave_type, **values
        )


def test_the_common_case_is_no_row_at_all(employee, annual):
    """Absence means the sectoral rule set applies untouched."""
    with tenant_context(employee.tenant_id):
        assert not EmployeeLeaveEntitlement.objects.filter(employee=employee).exists()


def test_an_additive_entitlement_sits_on_top_of_the_statute(employee, annual):
    row = make_entitlement(employee, annual, additional_days_per_cycle=Decimal("3.000"))

    assert row.replaces_statutory is False
    assert row.total_days_per_cycle_override is None
    assert row.additional_days_per_cycle == Decimal("3.000")


def test_a_replacing_entitlement_must_state_its_total(employee, annual):
    """Otherwise the total is NULL and the reader falls back to the statute silently."""
    with pytest.raises(IntegrityError), transaction.atomic():
        make_entitlement(employee, annual, replaces_statutory=True)


def test_an_additive_entitlement_may_not_also_carry_a_total(employee, annual):
    """THE EXPENSIVE CONFUSION. 21 additive on a statutory 15 is 36 days."""
    with tenant_context(employee.tenant_id):
        row = EmployeeLeaveEntitlement(
            tenant=employee.tenant,
            employee=employee,
            leave_type=annual,
            effective_from=START,
            replaces_statutory=False,
            total_days_per_cycle_override=Decimal("21.000"),
        )
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    assert "replaces_statutory" in str(raised.value)


def test_a_replacing_entitlement_may_not_also_add(employee, annual):
    with tenant_context(employee.tenant_id):
        row = EmployeeLeaveEntitlement(
            tenant=employee.tenant,
            employee=employee,
            leave_type=annual,
            effective_from=START,
            replaces_statutory=True,
            total_days_per_cycle_override=Decimal("21.000"),
            additional_days_per_cycle=Decimal("3.000"),
        )
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    assert "One or the other" in str(raised.value)


def test_two_entitlements_for_one_type_cannot_overlap(employee, annual):
    """Two answers to 'how many days', decided by row order."""
    make_entitlement(employee, annual, additional_days_per_cycle=Decimal("3.000"))

    with pytest.raises(IntegrityError), transaction.atomic():
        make_entitlement(
            employee, annual, effective_from=LATER, additional_days_per_cycle=Decimal("5.000")
        )


def test_a_closed_entitlement_leaves_room_for_the_next(employee, annual):
    """Invariant 2: raising leave in July does not rewrite what accrued in March."""
    make_entitlement(
        employee,
        annual,
        additional_days_per_cycle=Decimal("3.000"),
        effective_to=LATER,
    )
    later = make_entitlement(
        employee, annual, effective_from=LATER, additional_days_per_cycle=Decimal("5.000")
    )

    with tenant_context(employee.tenant_id):
        assert EmployeeLeaveEntitlement.objects.filter(employee=employee).count() == 2
    assert later.effective_from == LATER


def test_different_leave_types_may_overlap(employee, annual):
    """The exclusion is per type — extra annual and extra sick are not a conflict."""
    with platform_context():
        sick = LeaveType.objects.create(
            code=LeaveType.Code.SICK, name="Sick leave", cycle_months=36, is_system=True
        )

    make_entitlement(employee, annual, additional_days_per_cycle=Decimal("3.000"))
    make_entitlement(employee, sick, additional_days_per_cycle=Decimal("2.000"))

    with tenant_context(employee.tenant_id):
        assert EmployeeLeaveEntitlement.objects.filter(employee=employee).count() == 2


# --------------------------------------------------- recurring payslip lines


def make_recurring(employee, component, **overrides):
    values = {"effective_from": START, "amount": Decimal("100.0000"), **overrides}
    with tenant_context(employee.tenant_id):
        return EmployeeRecurringComponent.objects.create(
            tenant=employee.tenant, employee=employee, payroll_component=component, **values
        )


def test_a_line_states_an_amount_or_a_percentage(employee, tenant, source_code):
    allowance = make_component(
        tenant, "TRANSPORT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        make_recurring(employee, allowance, amount=None, percentage_of_basic=None)


def test_a_line_may_not_state_both(employee, tenant, source_code):
    """Two figures on one line means the payslip picks one and nobody can tell which."""
    allowance = make_component(
        tenant, "TRANSPORT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    with tenant_context(employee.tenant_id):
        row = EmployeeRecurringComponent(
            tenant=employee.tenant,
            employee=employee,
            payroll_component=allowance,
            effective_from=START,
            amount=Decimal("100.0000"),
            percentage_of_basic=Decimal("10.0000"),
        )
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    assert "not both" in str(raised.value)


def test_a_non_statutory_deduction_needs_written_consent_on_file(employee, tenant):
    """BCEA s34(1). 'He agreed' is what every one of these disputes turns out to be."""
    loan = make_component(tenant, "ADVANCE", PayrollComponent.ComponentType.DEDUCTION)

    with tenant_context(employee.tenant_id):
        row = EmployeeRecurringComponent(
            tenant=employee.tenant,
            employee=employee,
            payroll_component=loan,
            effective_from=START,
            amount=Decimal("250.0000"),
        )
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    assert "s34(1)" in str(raised.value)
    assert "ADVANCE" in str(raised.value)


def test_a_statutory_deduction_needs_no_consent(employee, tenant):
    """PAYE and UIF are deducted because a statute says so, not because anyone agreed.

    The signal is ``calculation_method == STATUTORY``, not ``is_system``: ACCOM_DED
    and ADVANCE_DED are system components too, and both still need a signature.
    """
    paye = make_component(
        tenant,
        "PAYE",
        PayrollComponent.ComponentType.DEDUCTION,
        method=PayrollComponent.CalculationMethod.STATUTORY,
    )
    with tenant_context(employee.tenant_id):
        row = EmployeeRecurringComponent(
            tenant=employee.tenant,
            employee=employee,
            payroll_component=paye,
            effective_from=START,
            amount=Decimal("0.0000"),
        )
        row.full_clean()  # must not raise


def test_only_a_deduction_runs_a_balance_down(employee, tenant, source_code):
    """An earning with a balance is a loan recorded the wrong way round."""
    allowance = make_component(
        tenant, "TRANSPORT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    with tenant_context(employee.tenant_id):
        row = EmployeeRecurringComponent(
            tenant=employee.tenant,
            employee=employee,
            payroll_component=allowance,
            effective_from=START,
            amount=Decimal("100.0000"),
            balance_outstanding=Decimal("500.00"),
        )
        with pytest.raises(ValidationError) as raised:
            row.full_clean()

    assert "the wrong way round" in str(raised.value)


def test_a_cap_is_a_percentage(employee, tenant, source_code):
    allowance = make_component(
        tenant, "TRANSPORT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        make_recurring(employee, allowance, total_deduction_cap_pct=Decimal("101.00"))


def test_one_component_cannot_be_live_twice(employee, tenant, source_code):
    """The employee would be paid the allowance, or charged the deduction, twice."""
    allowance = make_component(
        tenant, "TRANSPORT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    make_recurring(employee, allowance)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_recurring(employee, allowance, effective_from=LATER)


def test_two_different_components_may_run_together(employee, tenant, source_code):
    transport = make_component(
        tenant, "TRANSPORT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    shift = make_component(
        tenant, "SHIFT", PayrollComponent.ComponentType.EARNING, source_code=source_code
    )
    make_recurring(employee, transport)
    make_recurring(employee, shift)

    with tenant_context(employee.tenant_id):
        assert EmployeeRecurringComponent.objects.filter(employee=employee).count() == 2


# ------------------------------------------------------------------ file notes


def test_a_note_records_what_was_thought_at_the_time(employee):
    with tenant_context(employee.tenant_id):
        note = EmployeeNote.objects.create(
            tenant=employee.tenant,
            employee=employee,
            category=EmployeeNote.Category.TRAINING,
            subject="Chemical handling",
            body="Completed the supplier's half-day course.",
        )

    assert note.note_date == datetime.date.today()
    assert note.is_confidential is False


def test_a_note_needs_a_body(employee):
    """An empty note is a row that says somebody meant to write something."""
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(employee.tenant_id):
        EmployeeNote.objects.create(
            tenant=employee.tenant,
            employee=employee,
            subject="Meant to write this up",
            body="",
        )


def test_a_confidential_note_is_hidden_from_self_service_and_from_nothing_else(employee):
    """A visibility flag, not a security boundary.

    The employer's own staff read it, and a POPIA s23 subject access request reaches
    it like any other personal information. Anyone writing one should assume the
    employee will eventually read it, because they are entitled to.
    """
    with tenant_context(employee.tenant_id):
        note = EmployeeNote.objects.create(
            tenant=employee.tenant,
            employee=employee,
            category=EmployeeNote.Category.PERFORMANCE,
            body="Third late arrival this month. Spoke to her about it.",
            is_confidential=True,
        )
        assert EmployeeNote.objects.filter(pk=note.pk).exists()


def test_notes_read_newest_first(employee):
    with tenant_context(employee.tenant_id):
        for day in (1, 15, 8):
            EmployeeNote.objects.create(
                tenant=employee.tenant,
                employee=employee,
                note_date=datetime.date(2026, 6, day),
                body=f"Note on the {day}th.",
            )
        dates = list(
            EmployeeNote.objects.filter(employee=employee).values_list("note_date", flat=True)
        )

    assert dates == sorted(dates, reverse=True)
