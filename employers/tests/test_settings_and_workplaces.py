"""Employer settings, and resolving a workplace's wage area.

The setting tests are mostly about one line that must not be crossed: a statutory
figure must never become an employer preference. The area tests are about the
opposite failure — an unknown area quietly becoming a default, which underpays every
employee at a site and looks like nothing at all.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from core.managers import tenant_context
from core.models import Tenant
from employers.areas import apply_area, resolve_area
from employers.models import Employer, EmployerSetting, Workplace
from employers.onboarding import SETTING_DEFINITIONS, seed_settings_for, setting_value
from statutory.models import MunicipalityAreaMap, Sector, SectorArea

MARCH_2026 = datetime.date(2026, 3, 1)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="A subscriber")


@pytest.fixture
def domestic(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def cleaning(db):
    return Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING,
        name="Contract cleaning sector",
        uses_area_rates=True,
    )


def make_employer(tenant, sector, name="An Employer"):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name=name, sector=sector)


def make_workplace(tenant, employer, **overrides):
    values = {"name": "Main site", "municipality": "", **overrides}
    with tenant_context(tenant.pk):
        return Workplace.objects.create(tenant=tenant, employer=employer, **values)


# -------------------------------------------------------------------- settings


@pytest.mark.django_db
def test_onboarding_seeds_every_defined_setting(tenant, domestic):
    employer = make_employer(tenant, domestic)
    with tenant_context(tenant.pk):
        created = seed_settings_for(employer)

    assert len(created) == len(SETTING_DEFINITIONS)
    assert all(row.set_by_employer is False for row in created), (
        "A seeded value is not a chosen one, and the difference decides whether a "
        "changed sector default should reach this employer."
    )


@pytest.mark.django_db
def test_the_sort_default_follows_the_sector(tenant, domestic, cleaning):
    """A household knows Grace by her first name; a cleaning company runs a staff
    list by surname (D-16)."""
    household = make_employer(tenant, domestic, name="A Household")
    company = make_employer(tenant, cleaning, name="A Cleaning Company")

    with tenant_context(tenant.pk):
        seed_settings_for(household)
        seed_settings_for(company)

        assert setting_value(household, "EMPLOYEE_LIST_SORT") == "first_name"
        assert setting_value(company, "EMPLOYEE_LIST_SORT") == "surname"


@pytest.mark.django_db
def test_contract_cleaning_is_seeded_to_defer_to_its_gazetted_night_allowance(tenant, cleaning):
    """SD1 clause 16 gazettes 10 percent of the hourly wage, so the rule set governs
    and the employer's own figure must not be consulted."""
    company = make_employer(tenant, cleaning)
    with tenant_context(tenant.pk):
        seed_settings_for(company)
        assert setting_value(company, "NIGHT_ALLOWANCE_IS_STATUTORY") is True


@pytest.mark.django_db
def test_a_domestic_employer_must_set_their_own_night_allowance(tenant, domestic):
    """The BCEA requires an allowance and names no amount. Seeding a plausible figure
    would be inventing a statutory number; seeding zero says 'nobody has set this'."""
    household = make_employer(tenant, domestic)
    with tenant_context(tenant.pk):
        seed_settings_for(household)
        assert setting_value(household, "NIGHT_ALLOWANCE_IS_STATUTORY") is False
        assert setting_value(household, "NIGHT_ALLOWANCE_PCT_OF_HOURLY") == Decimal("0.000000")


@pytest.mark.django_db
def test_seeding_twice_does_not_overwrite_what_the_employer_chose(tenant, domestic):
    employer = make_employer(tenant, domestic)
    with tenant_context(tenant.pk):
        seed_settings_for(employer)
        row = EmployerSetting.objects.get(employer=employer, setting_key="EMPLOYEE_LIST_SORT")
        row.value_text = "surname"
        row.set_by_employer = True
        row.save()

        assert seed_settings_for(employer) == []
        assert setting_value(employer, "EMPLOYEE_LIST_SORT") == "surname"


@pytest.mark.django_db
def test_a_setting_carries_its_value_in_exactly_one_column(tenant, domestic):
    """Two populated value columns is a row whose meaning depends on which one the
    reader looked at first."""
    employer = make_employer(tenant, domestic)
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        EmployerSetting.objects.create(
            tenant=tenant,
            employer=employer,
            setting_key="CONFUSED",
            value_type=EmployerSetting.ValueType.TEXT,
            value_text="something",
            value_numeric=Decimal("1"),
        )


@pytest.mark.django_db
def test_a_numeric_setting_cannot_be_left_without_a_number(tenant, domestic):
    employer = make_employer(tenant, domestic)
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        EmployerSetting.objects.create(
            tenant=tenant,
            employer=employer,
            setting_key="EMPTY",
            value_type=EmployerSetting.ValueType.NUMERIC,
        )


@pytest.mark.django_db
def test_one_row_per_setting_per_employer(tenant, domestic):
    employer = make_employer(tenant, domestic)
    with tenant_context(tenant.pk):
        seed_settings_for(employer)
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        EmployerSetting.objects.create(
            tenant=tenant,
            employer=employer,
            setting_key="EMPLOYEE_LIST_SORT",
            value_type=EmployerSetting.ValueType.TEXT,
            value_text="surname",
        )


# ------------------------------------------------------------- area resolution


@pytest.mark.django_db
def test_a_domestic_workplace_has_no_area_to_resolve(tenant, domestic):
    employer = make_employer(tenant, domestic)
    workplace = make_workplace(tenant, employer, municipality="City of Cape Town")

    with tenant_context(tenant.pk):
        resolution = resolve_area(workplace, MARCH_2026)

    assert not resolution
    assert "does not use area rates" in resolution.reason


@pytest.mark.django_db
def test_a_cleaning_workplace_with_no_municipality_says_what_to_ask_for(tenant, cleaning):
    """Ask for the municipality, never for the area."""
    employer = make_employer(tenant, cleaning)
    workplace = make_workplace(tenant, employer)

    with tenant_context(tenant.pk):
        resolution = resolve_area(workplace, MARCH_2026)

    assert not resolution
    assert "municipality" in resolution.reason


@pytest.mark.django_db
def test_an_unmapped_municipality_resolves_to_nothing_rather_than_an_area(tenant, cleaning):
    """THE TEST THIS MODULE EXISTS FOR.

    municipality_area_map is empty today - no municipality data was ever loaded. A
    resolver that defaulted to Area A here would underpay or overpay every employee
    at the site and produce payslips that look entirely normal.
    """
    employer = make_employer(tenant, cleaning)
    workplace = make_workplace(tenant, employer, municipality="Somewhere Unmapped")

    with tenant_context(tenant.pk):
        resolution = resolve_area(workplace, MARCH_2026)

    assert not resolution
    assert resolution.area is None
    assert "blocks onboarding" in resolution.reason


@pytest.mark.django_db
def test_a_mapped_municipality_resolves_and_is_stored_with_its_date(tenant, cleaning):
    area = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")
    MunicipalityAreaMap.objects.create(
        sector_area=area,
        province_code="WC",
        municipality_name="City of Cape Town",
        effective_from=MARCH_2026,
    )

    employer = make_employer(tenant, cleaning)
    workplace = make_workplace(tenant, employer, municipality="City of Cape Town")

    with tenant_context(tenant.pk):
        resolution = apply_area(workplace, MARCH_2026)
        workplace.refresh_from_db()

    assert resolution.resolved
    assert workplace.sector_area == area
    assert workplace.area_resolved_on == MARCH_2026, (
        "The date is stored so a re-run reproduces this answer, not today's mapping."
    )


@pytest.mark.django_db
def test_the_mapping_is_read_as_it_stood_on_the_date(tenant, cleaning):
    """Municipal amalgamations have moved workplaces between areas. A 2026 payroll
    re-run in 2030 must use the 2026 mapping."""
    old_area = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")
    new_area = SectorArea.objects.create(sector=cleaning, code="AREA_B", name="Area B")
    MunicipalityAreaMap.objects.create(
        sector_area=old_area,
        province_code="GP",
        municipality_name="Somewhere",
        effective_from=MARCH_2026,
        effective_to=datetime.date(2027, 3, 1),
    )
    MunicipalityAreaMap.objects.create(
        sector_area=new_area,
        province_code="GP",
        municipality_name="Somewhere",
        effective_from=datetime.date(2027, 3, 1),
    )

    employer = make_employer(tenant, cleaning)
    workplace = make_workplace(tenant, employer, municipality="Somewhere")

    with tenant_context(tenant.pk):
        assert resolve_area(workplace, MARCH_2026).area == old_area
        assert resolve_area(workplace, datetime.date(2027, 6, 1)).area == new_area


@pytest.mark.django_db
def test_a_workplace_cannot_carry_an_area_without_the_date_it_was_resolved(tenant, cleaning):
    area = SectorArea.objects.create(sector=cleaning, code="AREA_A", name="Area A")
    employer = make_employer(tenant, cleaning)

    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        Workplace.objects.create(tenant=tenant, employer=employer, name="Undated", sector_area=area)


@pytest.mark.django_db
def test_two_workplaces_of_one_employer_cannot_share_a_name(tenant, cleaning):
    employer = make_employer(tenant, cleaning)
    make_workplace(tenant, employer, name="Site One")

    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant.pk):
        Workplace.objects.create(tenant=tenant, employer=employer, name="Site One")
