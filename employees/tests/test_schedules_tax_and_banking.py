"""Work schedules, tax profiles and employee banking.

Three tables that look like reference data and are not. Each one decides something a
payslip depends on, and each has a way of being captured that is individually
plausible and jointly wrong:

- two schedules in force at once, so a day is both an ordinary working day and not,
- a directive status with no directive number, which reaches the IRP5 as a blank,
- two live bank accounts, so the payment file picks one by row order.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import (
    Employee,
    EmployeeBankAccount,
    EmployeeTaxProfile,
    WorkSchedule,
    WorkScheduleDay,
)
from employees.remuneration import MONTHLY_FACTOR_PARAMETER, band_for, check_minimum_wage
from employers.models import Employer
from statutory.models import Bank, MinimumWageRate, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)


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
def bank(db):
    return Bank.objects.create(name="Test Bank", universal_branch_code="123456")


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


def make_schedule(employee, **overrides):
    values = {"effective_from": START, **overrides}
    with tenant_context(employee.tenant_id):
        return WorkSchedule.objects.create(tenant=employee.tenant, employee=employee, **values)


# ------------------------------------------------------------- work schedules


def test_a_schedule_carries_the_declared_wage_band(employee):
    over = make_schedule(employee, works_over_27_hours_week=True)
    assert over.hours_band == MinimumWageRate.HoursBand.GT_27

    with tenant_context(employee.tenant_id):
        over.delete()
    under = make_schedule(employee, works_over_27_hours_week=False)
    assert under.hours_band == MinimumWageRate.HoursBand.LTE_27


def test_the_wage_band_is_read_from_the_schedule_not_computed(employee):
    """THE RESOLUTION TO D-105.

    The band is a boolean the employer declares, not a comparison against 27. So a
    schedule claiming 45 hours a week and the lower band is honoured exactly as
    captured — because the gazette owns the threshold and this system does not know
    it. The combination is contradictory, and the point is that nothing here is
    entitled to decide that.
    """
    make_schedule(employee, ordinary_hours_per_week=Decimal(45), works_over_27_hours_week=False)
    with tenant_context(employee.tenant_id):
        assert band_for(employee, START) == MinimumWageRate.HoursBand.LTE_27


def test_an_employee_with_no_schedule_falls_back_to_the_all_band(employee):
    """Refusing to capture a rate until a schedule exists would block the screen that
    captures both."""
    with tenant_context(employee.tenant_id):
        assert band_for(employee, START) == MinimumWageRate.HoursBand.ALL


def test_two_schedules_cannot_be_in_force_at_once(employee):
    """A day would be both an ordinary working day and not one — which decides public
    holiday pay, the Sunday multiplier and how much leave a day consumes."""
    make_schedule(employee)
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        WorkSchedule.objects.create(
            tenant=employee.tenant, employee=employee, effective_from=datetime.date(2026, 6, 1)
        )


def test_a_schedule_day_off_cannot_carry_hours(employee):
    schedule = make_schedule(employee)
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        WorkScheduleDay.objects.create(
            tenant=employee.tenant,
            work_schedule=schedule,
            cycle_day=6,
            is_working_day=False,
            ordinary_hours=Decimal(8),
        )


def test_a_cycle_day_cannot_repeat_within_a_schedule(employee):
    schedule = make_schedule(employee)
    with tenant_context(employee.tenant_id):
        WorkScheduleDay.objects.create(
            tenant=employee.tenant,
            work_schedule=schedule,
            cycle_day=0,
            ordinary_hours=Decimal(8),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkScheduleDay.objects.create(
                tenant=employee.tenant,
                work_schedule=schedule,
                cycle_day=0,
                ordinary_hours=Decimal(9),
            )


def test_the_stored_hours_are_not_recomputed_from_the_times(employee):
    """An employer who says 08:00 to 17:00, a 60-minute break and 8.5 hours has an
    arrangement. Recomputing would overwrite what the employee agreed to."""
    schedule = make_schedule(employee)
    with tenant_context(employee.tenant_id):
        day = WorkScheduleDay.objects.create(
            tenant=employee.tenant,
            work_schedule=schedule,
            cycle_day=0,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(17, 0),
            unpaid_break_minutes=60,
            ordinary_hours=Decimal("8.50"),
        )
    assert day.ordinary_hours == Decimal("8.50")


def test_the_band_reaches_the_minimum_wage_check(employee, sector):
    """End to end: the declared band selects the gazetted row.

    Two rows for the same sector on the same date, differing only by band. Without the
    band reaching resolve, the ORM would return whichever came first.
    """
    MinimumWageRate.objects.create(
        sector=sector,
        hours_band=MinimumWageRate.HoursBand.GT_27,
        hourly_rate=Decimal("30.2300"),
        effective_from=START,
        source_reference="Test fixture, over 27 hours",
    )
    MinimumWageRate.objects.create(
        sector=sector,
        hours_band=MinimumWageRate.HoursBand.LTE_27,
        hourly_rate=Decimal("33.1000"),
        effective_from=START,
        source_reference="Test fixture, 27 hours or fewer",
    )
    make_schedule(employee, works_over_27_hours_week=False)

    with tenant_context(employee.tenant_id):
        check = check_minimum_wage(employee, hourly_rate=Decimal("32"), on_date=START)

    assert check.floor_hourly == Decimal("33.1000")
    assert not check.clears, "The part-time band is the higher rate here, and it applies."


# -------------------------------------------------------------- tax profiles


def make_tax_profile(employee, **overrides):
    values = {"effective_from": START, **overrides}
    with tenant_context(employee.tenant_id):
        return EmployeeTaxProfile.objects.create(
            tenant=employee.tenant, employee=employee, **values
        )


def test_the_nature_of_person_defaults_from_the_id_type():
    """A for a South African ID, B for anything else. C is never derived."""
    assert (
        EmployeeTaxProfile.nature_from_id_type(Employee.IdType.SA_ID)
        == EmployeeTaxProfile.NatureOfPerson.INDIVIDUAL_WITH_ID
    )
    assert (
        EmployeeTaxProfile.nature_from_id_type(Employee.IdType.PASSPORT)
        == EmployeeTaxProfile.NatureOfPerson.INDIVIDUAL_WITHOUT_ID
    )
    assert (
        EmployeeTaxProfile.nature_from_id_type(Employee.IdType.ASYLUM_PERMIT)
        == EmployeeTaxProfile.NatureOfPerson.INDIVIDUAL_WITHOUT_ID
    )


def test_a_director_still_uses_the_ordinary_tables(employee):
    """The flat 25% director rate was repealed in 2017.

    Asserted as a test because it is the most persistent piece of out-of-date South
    African payroll folklore, and the next person to touch directors will reach for
    a special case.
    """
    profile = make_tax_profile(
        employee, nature_of_person=EmployeeTaxProfile.NatureOfPerson.DIRECTOR
    )
    assert profile.tax_status == EmployeeTaxProfile.TaxStatus.STANDARD


def test_a_percentage_directive_needs_its_percentage(employee):
    profile = EmployeeTaxProfile(
        tenant=employee.tenant,
        employee=employee,
        tax_status=EmployeeTaxProfile.TaxStatus.DIRECTIVE_FIXED_PCT,
        directive_number="D123",
        effective_from=START,
    )
    with pytest.raises(ValidationError) as caught:
        profile.full_clean(exclude=["tenant"])
    assert "percentage" in str(caught.value)


def test_the_database_refuses_a_percentage_directive_with_no_percentage(employee):
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        EmployeeTaxProfile.objects.create(
            tenant=employee.tenant,
            employee=employee,
            tax_status=EmployeeTaxProfile.TaxStatus.DIRECTIVE_FIXED_PCT,
            effective_from=START,
        )


def test_a_directive_status_needs_its_number(employee):
    """SARS issues it per employee per year, and it goes on the IRP5 as a blank
    otherwise."""
    profile = EmployeeTaxProfile(
        tenant=employee.tenant,
        employee=employee,
        tax_status=EmployeeTaxProfile.TaxStatus.DIRECTIVE_FIXED_AMOUNT,
        directive_amount=Decimal("1500"),
        effective_from=START,
    )
    with pytest.raises(ValidationError) as caught:
        profile.full_clean(exclude=["tenant"])
    assert "directive number" in str(caught.value)


def test_a_uif_exemption_needs_a_reason(employee):
    """The UI-19 asks for it, and an unexplained exemption is the one an inspector
    opens with."""
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        EmployeeTaxProfile.objects.create(
            tenant=employee.tenant, employee=employee, is_uif_exempt=True, effective_from=START
        )


def test_the_under_24_hours_exemption_is_declared_not_computed(employee):
    """Same argument as the wage band (D-110). The Unemployment Insurance Act keeps
    its 24-hour threshold; the employer states the fact; no figure lives in code."""
    profile = make_tax_profile(
        employee,
        is_uif_exempt=True,
        uif_exempt_reason=EmployeeTaxProfile.UifExemptReason.UNDER_24_HOURS,
    )
    assert profile.is_uif_exempt is True


def test_two_tax_profiles_cannot_be_in_force_at_once(employee):
    make_tax_profile(employee)
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        EmployeeTaxProfile.objects.create(
            tenant=employee.tenant, employee=employee, effective_from=datetime.date(2026, 6, 1)
        )


def test_the_tax_reference_number_is_never_written_to_the_audit_trail(employee):
    """POPIA: protecting it on the record and writing it in clear into a table kept
    for years would be protecting nothing."""
    assert "tax_reference_number" in EmployeeTaxProfile.audit_sensitive_fields


# ------------------------------------------------------------- bank accounts


def make_account(employee, bank, **overrides):
    values = {
        "bank": bank,
        "branch_code": "123456",
        "account_number": "1234567890",
        "account_holder_name": "T Mokoena",
        "active_from": START,
        **overrides,
    }
    with tenant_context(employee.tenant_id):
        return EmployeeBankAccount.objects.create(
            tenant=employee.tenant, employee=employee, **values
        )


def test_an_account_number_is_encrypted_with_its_companions_filled(employee, bank):
    account = make_account(employee, bank)
    assert account.account_number_last4 == "7890"
    assert len(account.account_number_hash) == 64


def test_two_employees_on_one_account_are_findable(employee, bank, tenant, employer, parameters):
    """THE GHOST EMPLOYEE CHECK (D-111).

    Several 'employees' paid into one bank account is the classic payroll fraud in
    contract cleaning, which is half this market. A signal rather than a block:
    spouses and families legitimately share an account, so refusing the second one
    would be wrong more often than right.
    """
    with tenant_context(tenant.pk):
        second = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Sipho",
            last_name="Ndlovu",
            date_of_birth=BORN,
            mobile_number="+27820000002",
            email="sipho@example.com",
            id_number=make_id(sequence="5010"),
        )
    engage(second, start_date=START, job_title="Domestic worker")

    first_account = make_account(employee, bank)
    make_account(second, bank, account_holder_name="S Ndlovu")

    with tenant_context(tenant.pk):
        sharing = EmployeeBankAccount.objects.filter(
            account_number_hash=first_account.account_number_hash
        )
        assert sharing.count() == 2, "Both are visible, and neither was refused."


def test_the_hash_differs_between_tenants(employee, bank):
    """Scoped per tenant (D-95), so the column cannot say that two subscribers pay
    into the same account."""
    from core.db.fields import keyed_hash

    account = make_account(employee, bank)
    assert account.account_number_hash == keyed_hash(
        "1234567890", scope=f"tenant:{employee.tenant_id}"
    )
    assert account.account_number_hash != keyed_hash("1234567890", scope="tenant:99999")


def test_cash_is_a_first_class_payment_method(employee):
    """A domestic employer paying a weekly wage in cash still owes a payslip under
    BCEA s33. Refusing the arrangement would push them off the product, not into
    compliance."""
    account = make_account(
        employee,
        None,
        payment_method=EmployeeBankAccount.PaymentMethod.CASH,
        account_number="",
        branch_code="",
    )
    assert account.account_number_last4 == ""
    assert "Cash" in str(account)


def test_an_eft_with_no_account_number_is_refused_by_the_database(employee, bank):
    """The check is on the last4 companion, because the encrypted column cannot be
    compared to anything — Fernet ciphertext is non-deterministic and the field
    refuses the constraint outright rather than letting one that can never fire look
    like protection."""
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        EmployeeBankAccount.objects.create(
            tenant=employee.tenant,
            employee=employee,
            payment_method=EmployeeBankAccount.PaymentMethod.EFT,
            bank=bank,
            account_number="",
            active_from=START,
        )


def test_a_third_party_account_needs_written_consent(employee, bank):
    """BCEA s34 limits what may be done with an employee's wages, and a verbal
    arrangement is what this dispute always turns out to have been."""
    account = EmployeeBankAccount(
        tenant=employee.tenant,
        employee=employee,
        bank=bank,
        branch_code="123456",
        account_number="1234567890",
        account_holder_relationship=EmployeeBankAccount.HolderRelationship.SPOUSE,
        active_from=START,
    )
    with pytest.raises(ValidationError) as caught:
        account.full_clean(exclude=["tenant"])
    assert "written consent" in str(caught.value)


def test_only_one_account_can_be_live_at_a_time(employee, bank):
    """Two live accounts means the payment file picks one by row order."""
    make_account(employee, bank)
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        tenant_context(employee.tenant_id),
    ):
        EmployeeBankAccount.objects.create(
            tenant=employee.tenant,
            employee=employee,
            bank=bank,
            branch_code="123456",
            account_number="9999999999",
            active_from=datetime.date(2026, 6, 1),
        )


def test_a_closed_account_makes_room_for_the_next(employee, bank):
    first = make_account(employee, bank)
    with tenant_context(employee.tenant_id):
        EmployeeBankAccount.objects.filter(pk=first.pk).update(active_to=datetime.date(2026, 6, 1))
    second = make_account(
        employee, bank, account_number="9999999999", active_from=datetime.date(2026, 6, 1)
    )
    assert second.account_number_last4 == "9999"
