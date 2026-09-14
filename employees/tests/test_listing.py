"""The employee list: who is on it, in what order, under which heading.

Three rules meet here and they pull in different directions. The employer's order
governs every screen, because muscle memory built on one list is wrong on the next
(D-16). A user may still re-sort their own list and have it remembered (D-133). And
a household with two employees gets no group headings at all, because headings over
a two-row list are clutter and that list is the domestic employer's entire
experience of the product (D-19).

The order itself is the ICU-collated generated column, not a Python sort and not
``ORDER BY last_name``. ``test_the_order_is_the_icu_collations`` is the one that
proves it: *Böhmer* files with the B-o names under ``en-ZA-x-icu`` and after every
one of them under a byte comparison, and only one of those is where somebody
scanning a staff list will look for it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import AppUser, Tenant, TenantMembership
from employees.engagements import MINIMUM_AGE_PARAMETER, engage, terminate
from employees.identity import luhn_check_digit
from employees.listing import (
    FIRST_NAME,
    SURNAME,
    UnknownPreferenceError,
    default_sort,
    employee_list,
    remember_sort,
    remembered_sort,
    resolve_sort,
    set_preference,
)
from employees.models import Employee, EmployeeEngagement
from employees.remuneration import MONTHLY_FACTOR_PARAMETER, capture
from employers.models import Employer, EmployerSetting, PayGroup
from employers.onboarding import seed_settings_for
from statutory.models import MinimumWageRate, Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)
START = datetime.date(2026, 3, 1)
NMW_HOURLY = Decimal("30.2300")


def make_id(sequence):
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
def minimum_wage(db):
    return MinimumWageRate.objects.create(
        sector=None,
        hourly_rate=NMW_HOURLY,
        effective_from=START,
        source_reference="GN R.7083 in Government Gazette 54075, 3 February 2026",
    )


@pytest.fixture
def domestic(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Subscriber")


@pytest.fixture
def employer(db, tenant, domestic):
    with tenant_context(tenant.pk):
        made = Employer.objects.create(tenant=tenant, trading_name="Household", sector=domestic)
        seed_settings_for(made)
        return made


@pytest.fixture
def cleaning_employer(db, tenant, cleaning):
    with tenant_context(tenant.pk):
        made = Employer.objects.create(
            tenant=tenant, trading_name="Sparkle Cleaning", sector=cleaning
        )
        seed_settings_for(made)
        return made


@pytest.fixture
def membership(db, tenant):
    user = AppUser.objects.create_user(email="admin@example.com", password="x" * 16)
    with tenant_context(tenant.pk):
        return TenantMembership.objects.create(
            tenant=tenant, user=user, role=TenantMembership.Role.OWNER
        )


def make_group(tenant, employer, name, frequency):
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name=name,
            pay_frequency=frequency,
            first_period_start=START,
        )


def add(tenant, employer, first, last, sequence, *, parameters=None, engaged=True):
    with tenant_context(tenant.pk):
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name=first,
            last_name=last,
            date_of_birth=BORN,
            mobile_number=f"+2782000{sequence}",
            email=f"{first.lower()}.{sequence}@example.com",
            id_number=make_id(sequence),
        )
    if engaged:
        engage(person, start_date=START, job_title="Domestic worker")
    return person


# ------------------------------------------------------------------ the order


def test_a_household_sorts_on_first_names(employer):
    """She is Thandi, not Ms Mokoena. That is how a household employer thinks."""
    assert default_sort(employer) == FIRST_NAME


def test_a_contract_cleaner_sorts_on_surnames(cleaning_employer):
    """Sixty cleaners is a filing problem, and filing is by surname."""
    assert default_sort(cleaning_employer) == SURNAME


def test_the_employers_setting_beats_the_sector_default(employer):
    """Seeded from the sector, but theirs to change — and then it is theirs."""
    with tenant_context(employer.tenant_id):
        EmployerSetting.objects.filter(employer=employer, setting_key="EMPLOYEE_LIST_SORT").update(
            value_text=SURNAME, set_by_employer=True
        )

    assert default_sort(employer) == SURNAME


def test_a_user_may_re_sort_their_own_list_and_it_is_remembered(employer, membership):
    assert remembered_sort(membership) is None

    remember_sort(membership, SURNAME)

    with tenant_context(membership.tenant_id):
        membership.refresh_from_db()
    assert remembered_sort(membership) == SURNAME
    assert resolve_sort(employer, membership=membership) == SURNAME
    assert default_sort(employer) == FIRST_NAME, (
        "One person's view choice must not become the employer's order — that is "
        "what the attendance grid and the payroll run read (D-16)."
    )


def test_an_explicit_choice_beats_the_remembered_one(employer, membership):
    remember_sort(membership, SURNAME)
    assert resolve_sort(employer, membership=membership, sort=FIRST_NAME) == FIRST_NAME


def test_a_nonsense_sort_falls_back_rather_than_raising(employer, membership):
    """A stale bookmark or a hand-edited query string is not an error page."""
    assert resolve_sort(employer, membership=membership, sort="by_vibes") == FIRST_NAME


def test_the_preference_bag_refuses_a_key_it_does_not_know(membership):
    """A bag that accepts anything is where a setting that decides a figure ends up."""
    with pytest.raises(UnknownPreferenceError) as raised:
        set_preference(membership, "paye_rounding", "down")
    assert "registered UI preference" in str(raised.value)


def test_the_preference_bag_refuses_a_value_it_does_not_know(membership):
    with pytest.raises(UnknownPreferenceError):
        set_preference(membership, "employee_list_sort", "by_vibes")


def test_writing_one_preference_leaves_the_others_alone(membership):
    with tenant_context(membership.tenant_id):
        TenantMembership.objects.filter(pk=membership.pk).update(
            ui_preferences={"something_else": "kept"}
        )
        membership.refresh_from_db()

    remember_sort(membership, SURNAME)

    with tenant_context(membership.tenant_id):
        membership.refresh_from_db()
    assert membership.ui_preferences == {
        "something_else": "kept",
        "employee_list_sort": SURNAME,
    }


def test_the_order_is_the_icu_collations(tenant, employer, parameters):
    """THE ONE THAT LOOKS RANDOM TO A CUSTOMER.

    ``en-ZA-x-icu`` sorts an accented letter with its base letter, so *Böhmer*
    belongs between *Bezuidenhout* and *Botha*. Compared byte by byte it lands
    after both, because the UTF-8 for *ö* is numerically larger than any ASCII
    letter — the surname simply appears in the wrong place, with nothing on screen
    to say why. The collation is set in the CREATING migration (D-17); changing it
    afterwards rewrites the table and every index on it.
    """
    add(tenant, employer, "Anna", "Böhmer", "6001")
    add(tenant, employer, "Bea", "Botha", "6002")
    add(tenant, employer, "Cara", "Bezuidenhout", "6003")

    listed = employee_list(employer, sort=SURNAME).employees

    assert [e.last_name for e in listed] == ["Bezuidenhout", "Böhmer", "Botha"]


def test_case_does_not_split_the_order(tenant, employer, parameters):
    """Lower-cased in the expression, so ``De Villiers`` and ``de Villiers`` sit
    together rather than in two blocks."""
    add(tenant, employer, "Dan", "de Villiers", "6004")
    add(tenant, employer, "Eve", "De Waal", "6005")
    add(tenant, employer, "Fay", "Dlamini", "6006")

    listed = employee_list(employer, sort=SURNAME).employees

    assert [e.last_name for e in listed] == ["de Villiers", "De Waal", "Dlamini"]


# --------------------------------------------------------------- the grouping


def test_the_list_groups_by_pay_group(tenant, employer, parameters, minimum_wage):
    monthly = make_group(tenant, employer, "Monthly staff", PayGroup.PayFrequency.MONTHLY)
    weekly = make_group(tenant, employer, "Weekly cleaners", PayGroup.PayFrequency.WEEKLY)

    salaried = add(tenant, employer, "Thandi", "Mokoena", "6010")
    waged = add(tenant, employer, "Sipho", "Ndlovu", "6011")
    capture(
        salaried,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        pay_group=monthly,
        as_at=START,
    )
    capture(
        waged,
        pay_basis="weekly",
        rate_amount=Decimal("1600"),
        effective_from=START,
        pay_group=weekly,
        as_at=START,
    )

    listed = employee_list(employer)

    assert [section.heading for section in listed.sections] == [
        "Monthly staff",
        "Weekly cleaners",
    ]
    assert listed.count == 2


def test_an_employee_with_no_rate_yet_lands_at_the_bottom(
    tenant, employer, parameters, minimum_wage
):
    """Not at the top above everybody who is paid — a NULL is 'not captured yet'."""
    monthly = make_group(tenant, employer, "Monthly staff", PayGroup.PayFrequency.MONTHLY)
    paid = add(tenant, employer, "Thandi", "Mokoena", "6020")
    add(tenant, employer, "Unpaid", "Person", "6021")
    capture(
        paid,
        pay_basis="monthly",
        rate_amount=Decimal("6000"),
        effective_from=START,
        pay_group=monthly,
        as_at=START,
    )

    listed = employee_list(employer)

    assert [section.heading for section in listed.sections] == [
        "Monthly staff",
        "No pay group",
    ]


def test_a_two_person_household_gets_no_group_headings(tenant, employer, parameters, minimum_wage):
    """D-19. Headings over a two-row list are clutter."""
    monthly = make_group(tenant, employer, "Monthly staff", PayGroup.PayFrequency.MONTHLY)
    for index, (first, last) in enumerate([("Thandi", "Mokoena"), ("Sipho", "Ndlovu")]):
        person = add(tenant, employer, first, last, f"603{index}")
        capture(
            person,
            pay_basis="monthly",
            rate_amount=Decimal("6000"),
            effective_from=START,
            pay_group=monthly,
            as_at=START,
        )

    listed = employee_list(employer)

    assert listed.count == 2
    assert listed.show_group_headings is False, "Below GROUP_HEADER_MINIMUM (D-19)."


def test_a_bigger_list_gets_them(tenant, employer, parameters, minimum_wage):
    monthly = make_group(tenant, employer, "Monthly staff", PayGroup.PayFrequency.MONTHLY)
    weekly = make_group(tenant, employer, "Weekly cleaners", PayGroup.PayFrequency.WEEKLY)
    for index in range(4):
        person = add(tenant, employer, f"Person{index}", f"Surname{index}", f"604{index}")
        capture(
            person,
            pay_basis="monthly" if index % 2 else "weekly",
            rate_amount=Decimal("6000") if index % 2 else Decimal("1600"),
            effective_from=START,
            pay_group=monthly if index % 2 else weekly,
            as_at=START,
        )

    listed = employee_list(employer)

    assert listed.count == 4
    assert listed.show_group_headings is True


# ------------------------------------------------------------ who is on the list


def test_a_terminated_employee_drops_off_the_list_but_not_out_of_the_database(
    tenant, employer, parameters
):
    """They keep their IRP5 and their payslips. They are just not on this screen."""
    leaver = add(tenant, employer, "Gone", "Yesterday", "6050")
    add(tenant, employer, "Still", "Here", "6051")
    with tenant_context(tenant.pk):
        engagement = EmployeeEngagement.objects.get(employee=leaver)
    terminate(
        engagement,
        termination_date=datetime.date(2026, 3, 31),
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
    )

    assert [e.last_name for e in employee_list(employer).employees] == ["Here"]
    assert len(employee_list(employer, include_terminated=True).employees) == 2


def test_one_employers_list_holds_only_their_own_people(
    tenant, employer, cleaning_employer, parameters
):
    """Two employers under one subscriber — a bookkeeper's ordinary case."""
    add(tenant, employer, "Household", "Worker", "6060")
    add(tenant, cleaning_employer, "Contract", "Cleaner", "6061")

    assert [e.last_name for e in employee_list(employer).employees] == ["Worker"]
    assert [e.last_name for e in employee_list(cleaning_employer).employees] == ["Cleaner"]


def test_asking_to_remember_stores_the_order_used(employer, membership, tenant, parameters):
    add(tenant, employer, "Thandi", "Mokoena", "6070")

    employee_list(employer, membership=membership, sort=SURNAME, remember=True)

    with tenant_context(membership.tenant_id):
        membership.refresh_from_db()
    assert remembered_sort(membership) == SURNAME
