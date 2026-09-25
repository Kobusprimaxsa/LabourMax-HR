"""The annual bonus cycle against the database (D-286).

The arithmetic is ``calculators/tests/test_bonus.py``'s. What is proved here is
what the caller adds: which rule applies, which engagement and wages feed it,
which elections are read, that the cache rebuilds rather than drifts — and,
first, that an employer with no statutory bonus gets NO ROWS AT ALL.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from calculators.termination import TerminationInput, termination_payout
from calculators.tests.test_termination_statute import FOUR_MONTHS
from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeEngagement, EmployeeRemuneration
from employees.remuneration import MONTHLY_FACTOR_PARAMETER
from employers.models import Employer, EmployerSetting, PayGroup
from payroll import bonus
from payroll.models import AnnualBonusCycle
from statutory.models import Sector, SectorArea, StatutoryParameter, TerminationRuleSet

pytestmark = pytest.mark.django_db

SD1_WEEKLY = Decimal("1497.15")  # SD1 Area A from 1 March 2026, GN R.7083 clause 3(1)
ENGAGED = datetime.date(2024, 2, 1)


def a_rule_set(sector=None, area=None, *, weeks, month, pro_rata=True):
    return TerminationRuleSet.objects.create(
        sector=sector,
        sector_area=area,
        effective_from=datetime.date(2020, 1, 1),
        source_reference="Test rule set",
        severance_weeks_per_completed_year=Decimal("1.00"),
        severance_requires_operational_reason=True,
        annual_bonus_weeks=Decimal(weeks),
        annual_bonus_month=month,
        annual_bonus_pro_rata_on_termination=pro_rata,
        annual_bonus_min_service_months=0,
    )


@pytest.fixture
def reference(db):
    return load_bonus_reference()


def load_bonus_reference():
    StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("15.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="BCEA 75 of 1997, s43(1)",
    )
    StatutoryParameter.objects.create(
        parameter_code=MONTHLY_FACTOR_PARAMETER,
        value_numeric=Decimal("4.333333"),
        unit=StatutoryParameter.Unit.RATIO,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="BCEA 75 of 1997, s35(3)",
    )
    domestic = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    cleaning = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    area_b = SectorArea.objects.create(sector=cleaning, code="AREA_B", name="Area B")
    # The BCEA default: no payment month, no weeks. This is what a domestic
    # employer resolves to, SD7 stating no bonus either.
    a_rule_set(weeks="0.000", month=None, pro_rata=False)
    a_rule_set(cleaning, weeks="4.333", month=12)
    a_rule_set(cleaning, area_b, weeks="4.330", month=12)
    return {"domestic": domestic, "cleaning": cleaning, "area_b": area_b}


_ids = iter(range(100, 999))


def an_employer(sector, area=None, name="Employer"):
    tenant = Tenant.objects.create(trading_name=name)
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name=name, sector=sector, sector_area=area
        )
        pay_group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Weekly staff",
            pay_frequency=PayGroup.PayFrequency.WEEKLY,
            first_period_start=datetime.date(2024, 1, 1),
        )
    return employer, pay_group


def an_employee(employer, pay_group, *, start=ENGAGED, weekly=SD1_WEEKLY, **engagement):
    n = next(_ids)
    body = f"9001015{n:03d}08"[:12]
    with tenant_context(employer.tenant_id):
        employee = Employee.objects.create(
            tenant=employer.tenant,
            employer=employer,
            first_name="Nomsa",
            last_name=f"Dlamini{n}",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number=f"+2782000{n:04d}",
            email=f"nomsa{n}@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
    engaged = engage(employee, start_date=start, job_title="Cleaner", **engagement)
    with tenant_context(employer.tenant_id):
        EmployeeRemuneration.objects.create(
            tenant=employer.tenant,
            employee=employee,
            engagement=engaged,
            pay_group=pay_group,
            pay_basis=EmployeeRemuneration.PayBasis.WEEKLY,
            rate_amount=weekly,
            derived_hourly_rate=(weekly / 45).quantize(Decimal("0.000001")),
            derived_daily_rate=(weekly / 5).quantize(Decimal("0.000001")),
            derived_monthly_rate=(weekly * Decimal("4.333333")).quantize(Decimal("0.000001")),
            effective_from=start,
        )
    return employee


def rows_of(employer):
    with tenant_context(employer.tenant_id):
        return list(AnnualBonusCycle.objects.filter(employee__employer=employer))


# ============================================ an employer with no bonus


def test_an_employer_with_no_statutory_bonus_gets_no_rows_at_all(reference):
    """The brief's own proof. A household employing a domestic worker for two
    years: the instrument gives no bonus, so the accrual writes NOTHING — not a
    row of nil, which would read as a bonus that came to zero."""
    employer, pay_group = an_employer(reference["domestic"], name="Household")
    employee = an_employee(employer, pay_group, weekly=Decimal("1100.00"))

    for month_end in (datetime.date(2026, m, 28) for m in range(1, 13)):
        assert bonus.accrue(employer, as_at=month_end) == []

    assert rows_of(employer) == []
    assert bonus.bonus_input(employee, as_at=datetime.date(2026, 6, 30)) is None
    assert bonus.termination_bonus(employee, datetime.date(2026, 6, 20)) is None


# ========================================================= SD1 accrual


def test_sd1_accrues_a_twelfth_per_full_month(reference):
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)

    (row,) = bonus.accrue(employer, as_at=datetime.date(2026, 5, 31))

    assert (row.cycle_start, row.cycle_end) == (
        datetime.date(2026, 1, 1),
        datetime.date(2026, 12, 31),
    )
    assert row.full_months_worked == 5
    assert row.bonus_weeks == Decimal("4.333")
    # 5 / 12 × 4,333 × 1 497,15 — the mid-year leaver's figure, as an accrual.
    assert row.accrued_amount_exact == Decimal("2702.979563")
    assert row.accrued_amount == Decimal("2702.98")
    assert row.status == AnnualBonusCycle.Status.ACCRUING


def test_the_accrual_is_a_rebuild_not_an_increment(reference):
    """Twice on one date changes nothing; a back-dated increase moves the row,
    because it is recomputed from the remuneration history (invariant 3)."""
    employer, pay_group = an_employer(reference["cleaning"])
    employee = an_employee(employer, pay_group)
    as_at = datetime.date(2026, 5, 31)

    first = bonus.accrue(employer, as_at=as_at)
    second = bonus.accrue(employer, as_at=as_at)
    assert len(rows_of(employer)) == 1
    assert first[0].accrued_amount_exact == second[0].accrued_amount_exact

    with tenant_context(employer.tenant_id):
        EmployeeRemuneration.objects.filter(employee=employee).update(
            rate_amount=Decimal("1600.00")
        )
    (moved,) = bonus.accrue(employer, as_at=as_at)
    assert moved.accrued_amount_exact == Decimal("2888.666667"), "5/12 × 4,333 × 1 600"


def test_the_accrual_pins_the_tenant_itself(reference):
    """A month-end job runs with nothing pinned — which is how every test here
    calls it."""
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)
    assert len(bonus.accrue(employer, as_at=datetime.date(2026, 5, 31))) == 1


def test_without_the_pin_the_accrual_finds_nobody(reference, monkeypatch):
    """PROVE EVERY GUARD FAILS: take the employer's pin away and the employee
    query returns nobody under RLS — an accrual that silently accrued nothing."""
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)
    monkeypatch.setattr(bonus, "tenant_context_of", lambda _row: contextlib.nullcontext())

    assert bonus.accrue(employer, as_at=datetime.date(2026, 5, 31)) == []


def test_a_paid_row_is_not_rebuilt(reference):
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)
    (row,) = bonus.accrue(employer, as_at=datetime.date(2026, 5, 31))
    with tenant_context(employer.tenant_id):
        AnnualBonusCycle.objects.filter(pk=row.pk).update(status=AnnualBonusCycle.Status.FORFEITED)

    assert bonus.accrue(employer, as_at=datetime.date(2026, 6, 30)) == []
    with tenant_context(employer.tenant_id):
        assert AnnualBonusCycle.objects.get(pk=row.pk).full_months_worked == 5


# ================================================ the mid-year leaver, end to end


def test_a_mid_year_leavers_payout_through_the_database(reference):
    """The calculator test's hand-computed R2 702,98, reached from the rows: the
    engagement's own dates, the remuneration row, SD1's rule set."""
    employer, pay_group = an_employer(reference["cleaning"])
    employee = an_employee(employer, pay_group)
    leaving = datetime.date(2026, 6, 20)
    with tenant_context(employer.tenant_id):
        EmployeeEngagement.objects.filter(employee=employee).update(
            termination_date=leaving,
            termination_reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        )

    data = bonus.termination_bonus(employee, leaving)
    result = termination_payout(
        TerminationInput(
            calculated_for=leaving,
            weekly_rate=SD1_WEEKLY,
            daily_rate=Decimal("299.43"),
            hourly_rate=Decimal("33.27"),
            days_per_week=Decimal("5"),
            hours_per_week=Decimal("45"),
            pro_rata_minimum_service_months=FOUR_MONTHS,
            annual_bonus=data,
        )
    )

    assert data.is_termination and data.service_end == leaving
    assert result.pro_rata_bonus.rounded == Decimal("2702.98")
    # And the cache agrees with the payout: the accrual for the same leaver.
    (row,) = bonus.accrue(employer, as_at=datetime.date(2026, 6, 30))
    assert row.accrued_amount == result.pro_rata_bonus.rounded
    assert row.accrued_as_at == leaving


# ============================================================ BCCCI elections


def a_kzn_cleaner(reference, **engagement):
    employer, pay_group = an_employer(reference["cleaning"], reference["area_b"], name="KZN")
    employee = an_employee(employer, pay_group, start=datetime.date(2026, 2, 15), **engagement)
    return employer, employee


def elect(employer, key, value: bool):
    with tenant_context(employer.tenant_id):
        EmployerSetting.objects.update_or_create(
            tenant=employer.tenant,
            employer=employer,
            setting_key=key,
            defaults={
                "value_type": EmployerSetting.ValueType.BOOLEAN,
                "value_boolean": value,
                "set_by_employer": True,
            },
        )


def test_the_bccci_rule_is_the_area_scoped_one(reference):
    employer, employee = a_kzn_cleaner(reference)
    data = bonus.bonus_input(employee, as_at=datetime.date(2026, 12, 31))

    assert data.rule.weeks == Decimal("4.330")
    assert data.rate_basis.value == "each_month", "4.5(d)'s gazetted default"
    assert data.part_first_month_counts is False, "4.5(c)(ii)'s gazetted default"


def test_an_sd1_employee_is_never_read_the_bccci_elections(reference):
    """SD1 grants no discretion, so a setting cannot change an SD1 figure."""
    employer, pay_group = an_employer(reference["cleaning"])
    employee = an_employee(employer, pay_group, start=datetime.date(2026, 2, 15))
    elect(employer, "BONUS_PART_MONTH_EARNS_NOTHING", False)

    data = bonus.bonus_input(employee, as_at=datetime.date(2026, 12, 31))
    assert data.part_first_month_counts is False
    assert data.rate_basis.value == "at_the_end"


def test_the_same_election_does_move_a_kzn_cleaner(reference):
    """The other half of the test above, so it cannot pass because the setting
    never landed: the identical election counts a KwaZulu-Natal joining month."""
    employer, employee = a_kzn_cleaner(reference)
    elect(employer, "BONUS_PART_MONTH_EARNS_NOTHING", False)

    data = bonus.bonus_input(employee, as_at=datetime.date(2026, 12, 31))
    assert data.part_first_month_counts is True


def test_a_bccci_casual_does_not_qualify_by_default(reference):
    employer, employee = a_kzn_cleaner(
        reference, contract_type=EmployeeEngagement.ContractType.CASUAL
    )
    data = bonus.bonus_input(employee, as_at=datetime.date(2026, 12, 31))

    assert data.qualifies is False
    assert "4.5(f)" in data.disqualified_because


def test_only_one_area_scoped_termination_instrument_is_shipped():
    """``payroll/bonus.py`` recognises the BCCCI by its area scope. If a second
    area-scoped instrument is ever loaded, the elections would be read for it
    too — so this fails first, naming the file."""
    reference_dir = pathlib.Path(__file__).resolve().parents[2] / "reference"
    scoped = set()
    for path in reference_dir.glob("*.json"):
        tables = json.loads(path.read_text(encoding="utf-8")).get("tables", {})
        for row in tables.get("termination_rule_set", []):
            if row.get("sector_area"):
                assert "Contract Cleaning Services Industry (KZN)" in row["source_reference"], (
                    path.name
                )
                scoped.add(path.name)
    assert scoped, "the BCCCI termination rule sets are area-scoped and must be found"


# ================================================================ constraints


def test_a_paid_status_must_name_its_run(reference):
    """PROVE EVERY GUARD FAILS: the CHECK over the nullable run column."""
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)
    (row,) = bonus.accrue(employer, as_at=datetime.date(2026, 5, 31))

    with pytest.raises(IntegrityError, match="bonus_cycle_a_payment_names_its_run"):
        with transaction.atomic(), tenant_context(employer.tenant_id):
            AnnualBonusCycle.objects.filter(pk=row.pk).update(status=AnnualBonusCycle.Status.PAID)


def test_the_rounded_amount_must_be_the_exact_one_rounded(reference):
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)
    (row,) = bonus.accrue(employer, as_at=datetime.date(2026, 5, 31))

    with pytest.raises(IntegrityError, match="bonus_cycle_amount_is_the_exact_amount_rounded"):
        with transaction.atomic(), tenant_context(employer.tenant_id):
            AnnualBonusCycle.objects.filter(pk=row.pk).update(accrued_amount=Decimal("2702.97"))


def test_thirteen_months_in_a_cycle_is_refused(reference):
    employer, pay_group = an_employer(reference["cleaning"])
    an_employee(employer, pay_group)
    (row,) = bonus.accrue(employer, as_at=datetime.date(2026, 5, 31))

    with pytest.raises(IntegrityError, match="bonus_cycle_months_within_a_year"):
        with transaction.atomic(), tenant_context(employer.tenant_id):
            AnnualBonusCycle.objects.filter(pk=row.pk).update(full_months_worked=13)
