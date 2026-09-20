"""The 28-day long-service band, wired to the engine that spends it (D-267).

D-243 added four ``leave_rule_set`` columns and ``resolve.annual_leave_days()``
for BCCCI clause 9.1(b) — 28 consecutive days for MORE THAN ten years' service
against clause 9.1(a)'s 21 for everyone else — and **nothing read them**.
``leave/cycles.py`` went on reading ``annual_leave_days_per_cycle_*`` directly,
so a KwaZulu-Natal cleaner of twelve years' standing had a cycle entitlement of
18 days written against them, silently, by the only code that decides how much
leave they get.

**Two separate defects, and the second is the worse one.** The band was not
read. And ``_statutory_entitlement_quantity`` passed no ``sector_area`` at all,
so for that same employee it resolved Sectoral Determination 1's rule set
rather than the agreement that binds them (D-266's shape, in the place where it
costs leave days rather than a coincidence).

**The mid-cycle crossing turns out not to arise**, and the tests at the bottom
say why rather than leaving it to be rediscovered: a cycle is twelve months
anchored to the current engagement's start date (D-163), and service for the
band is counted from that same date, so the ten-year mark always lands exactly
on a cycle boundary. The question "what if somebody crosses in month seven"
has no answer because it has no instance — until a rule set arrives whose cycle
is not twelve months, which is the one case the code refuses rather than
guessing at.
"""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import engage
from employees.models import Employee
from employers.models import Employer
from leave import cycles
from statutory import resolve
from statutory.models import LeaveRuleSet, Sector, SectorArea

from .conftest import BORN, make_id

pytestmark = [pytest.mark.django_db]

#: Eleven years before the cycle under test, so the employee is past the band.
ELEVEN_YEARS = datetime.date(2015, 3, 1)
#: Exactly ten years. "More than ten" loads FALSE, so this is still ordinary.
TEN_YEARS = datetime.date(2016, 3, 1)
AS_AT = datetime.date(2026, 3, 1)


@pytest.fixture
def cleaning(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    area_b = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    return sector, area_b


def a_rule_set(sector, area, *, band, five, six, long_five=None, long_six=None, months=12):
    return LeaveRuleSet.objects.create(
        sector=sector,
        sector_area=area,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Test fixture",
        annual_leave_days_per_cycle_5day=Decimal(five),
        annual_leave_days_per_cycle_6day=Decimal(six),
        annual_accrual_days_per_month_5day=Decimal("1.25"),
        annual_accrual_days_per_month_6day=Decimal("1.5"),
        annual_accrual_ratio_days_worked=17,
        annual_accrual_ratio_hours_worked=17,
        annual_leave_cycle_months=months,
        annual_leave_forfeit_months=6,
        annual_leave_payable_on_termination=True,
        has_long_service_annual_leave=band,
        long_service_annual_leave_years=10 if band else None,
        long_service_years_inclusive=False if band else None,
        long_service_annual_leave_days_5day=Decimal(long_five) if band else None,
        long_service_annual_leave_days_6day=Decimal(long_six) if band else None,
        sick_leave_cycle_months=36,
        sick_leave_weeks_equivalent=Decimal("6"),
        sick_leave_first_six_months_ratio=26,
        sick_leave_payable_on_termination=False,
        family_responsibility_days=3,
        family_resp_min_service_months=4,
        family_resp_min_days_per_week=4,
        parental_leave_total_months=4,
        parental_leave_additional_days=10,
        parental_leave_shareable=True,
        maternity_earliest_start_weeks_before_birth=4,
        maternity_no_work_weeks_after_birth=6,
    )


@pytest.fixture
def kzn(db, tenant, cleaning, leave_rules, minimum_age):
    """A KwaZulu-Natal contract cleaning employer, with BOTH rule sets loaded —
    Sectoral Determination 1 sector-wide and the BCCCI agreement for Area B.
    Loading only the narrow one would let a test pass that never reached it."""
    sector, area_b = cleaning
    # SD1: no long-service band, and DIFFERENT ordinary figures so the two can
    # be told apart at all.
    a_rule_set(sector, None, band=False, five="15", six="18")
    a_rule_set(sector, area_b, band=True, five="16", six="19", long_five="20", long_six="24")

    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name="Spotless KZN", sector=sector, sector_area=area_b
        )
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Sipho",
            last_name="Ndlovu",
            date_of_birth=BORN,
            mobile_number="+27820000009",
            email="sipho@example.com",
            id_number=make_id("5011"),
        )
    return employee


def a_schedule(tenant, employee, days, *, effective_from=ELEVEN_YEARS):
    """The conftest schedule fixtures attach to the conftest employee, so this
    suite builds its own — a six-day week is what makes the band's own 6day
    column the one being read."""
    from employees.models import WorkSchedule, WorkScheduleDay

    with tenant_context(tenant.pk):
        made = WorkSchedule.objects.create(
            tenant=tenant,
            employee=employee,
            days_per_week=Decimal(days),
            ordinary_hours_per_week=Decimal(8 * days),
            effective_from=effective_from,
        )
        for cycle_day in range(7):
            WorkScheduleDay.objects.create(
                tenant=tenant,
                work_schedule=made,
                cycle_day=cycle_day,
                is_working_day=cycle_day < days,
                ordinary_hours=Decimal("8") if cycle_day < days else Decimal("0"),
            )
    return made


def entitlement(employee, annual_type, on_date):
    """Pins the tenant, because the work schedule lookup inside is a query and
    an unpinned one comes back empty rather than refused — the employee would
    silently read as a five-day week. ``ensure_cycles`` opens its own context;
    a direct call has to open one too."""
    with tenant_context(employee.tenant_id):
        return cycles.entitlement_quantity_for(employee, annual_type, None, on_date=on_date)


# ------------------------------------------------- the band, and the area


def test_twelve_years_in_kwazulu_natal_earns_the_long_service_band(kzn, annual_type, tenant):
    """THE CASE THIS EXISTS FOR. Clause 9.1(b). Before this, the cycle written
    against this employee said 18 days — SD1's six-day figure, from an
    instrument that does not bind them, without the band that does."""
    a_schedule(tenant, kzn, 6)
    engage(kzn, start_date=ELEVEN_YEARS, job_title="Cleaner")

    assert entitlement(kzn, annual_type, AS_AT) == Decimal("24")


def test_the_cycle_that_ends_on_the_tenth_anniversary_is_ordinary(kzn, annual_type, tenant):
    """``long_service_years_inclusive`` is FALSE because the clause says "more
    than ten years" — which side of the boundary the exact count falls on is
    DATA, per D-158, and never a convention applied in code. This cycle's last
    day is the day BEFORE the tenth anniversary, so it is ordinary."""
    a_schedule(tenant, kzn, 6)
    engage(kzn, start_date=AS_AT - relativedelta(years=9), job_title="Cleaner")

    assert entitlement(kzn, annual_type, AS_AT) == Decimal("19")


def test_the_cycle_that_starts_on_the_tenth_anniversary_carries_the_band(kzn, annual_type, tenant):
    """THE MID-CYCLE DECISION, AND THE WHOLE REASON IT HAD TO BE MADE (D-267).

    A twelve-month cycle anchored to the engagement start puts the tenth
    anniversary ON a cycle's first day, and "more than ten years" excludes that
    day — so measuring service at the cycle's OPENING would make this cycle
    ordinary and hand the employee their band a full year late, at eleven years
    of service. The service that earns a cycle's entitlement is the service the
    employee has in respect of that cycle, which is known when it closes.

    The instrument is still chosen as at the cycle's first day (D-107): a
    gazette published part-way through must not retroactively move a figure
    already accrued against. Two questions, two dates, deliberately.
    """
    a_schedule(tenant, kzn, 6)
    engage(kzn, start_date=AS_AT - relativedelta(years=10), job_title="Cleaner")

    assert entitlement(kzn, annual_type, AS_AT) == Decimal("24")


def test_a_five_day_week_reads_the_five_day_column_of_the_band(kzn, annual_type, tenant):
    a_schedule(tenant, kzn, 5)
    engage(kzn, start_date=ELEVEN_YEARS, job_title="Cleaner")

    assert entitlement(kzn, annual_type, AS_AT) == Decimal("20")


def test_the_area_rule_set_wins_over_the_sector_one(kzn, annual_type, tenant):
    """Watched NOT firing on the band: a short-service Area B employee still
    reads the AGREEMENT's ordinary figure (19), not the determination's (18).
    Without this, passing sector_area could be omitted and the band test above
    would still pass wherever the two ordinary figures happened to agree."""
    a_schedule(tenant, kzn, 6)
    engage(kzn, start_date=datetime.date(2025, 3, 1), job_title="Cleaner")

    assert entitlement(kzn, annual_type, AS_AT) == Decimal("19")


def test_an_employer_outside_area_b_is_untouched(db, tenant, cleaning, annual_type, minimum_age):
    """Areas A and C still read Sectoral Determination 1, band and all."""
    sector, area_b = cleaning
    a_rule_set(sector, None, band=False, five="15", six="18")
    a_rule_set(sector, area_b, band=True, five="16", six="19", long_five="20", long_six="24")
    area_c = SectorArea.objects.create(sector=sector, code="AREA_C", name="Area C")

    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name="Spotless Cape", sector=sector, sector_area=area_c
        )
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Nomsa",
            last_name="Dlamini",
            date_of_birth=BORN,
            mobile_number="+27820000010",
            email="nomsa@example.com",
            id_number=make_id("5012"),
        )
    a_schedule(tenant, employee, 6)
    engage(employee, start_date=ELEVEN_YEARS, job_title="Cleaner")

    assert entitlement(employee, annual_type, AS_AT) == Decimal("18")


# ----------------------------------------- the crossing, and why it does not arise


def test_the_band_is_reached_exactly_on_a_cycle_boundary_and_never_inside_one(
    kzn, annual_type, tenant
):
    """THE MID-CYCLE QUESTION, ANSWERED BY SHOWING IT HAS NO INSTANCE.

    A cycle is twelve months anchored to the current engagement's start date
    (D-163) and service for the band is counted from that same date, so the
    tenth anniversary IS a cycle boundary. Cycle 10 runs from nine to ten years
    and gets the ordinary figure; cycle 11 opens at ten years and one day of
    service and gets the band. Nothing is ever recomputed part-way through a
    cycle, so nothing has to decide what a part-way crossing is worth.
    """
    a_schedule(tenant, kzn, 6)
    engage(kzn, start_date=ELEVEN_YEARS, job_title="Cleaner")
    made = cycles.ensure_cycles(kzn, annual_type, horizon=ELEVEN_YEARS + relativedelta(years=11))

    by_number = {cycle.cycle_number: cycle for cycle in made}
    assert by_number[10].cycle_start == ELEVEN_YEARS + relativedelta(years=9)
    assert by_number[11].cycle_start == ELEVEN_YEARS + relativedelta(years=10)

    assert by_number[10].entitlement_quantity == Decimal("19.000"), (
        "the cycle that ENDS on the tenth anniversary is ordinary"
    )
    assert by_number[11].entitlement_quantity == Decimal("24.000"), (
        "the cycle that STARTS the day the band is reached carries it in full"
    )


def test_a_cycle_that_is_not_twelve_months_refuses_rather_than_choosing_a_side(
    db, tenant, cleaning, annual_type, minimum_age
):
    """The alignment above is a fact about twelve-month cycles, not a law. A
    rule set stating a different cycle length breaks it and makes the mid-cycle
    crossing reachable — at which point there IS a question, the instrument
    does not answer it, and this codebase does not invent answers.

    Refusing looks like a regression and is the opposite: it is the difference
    between not knowing and being confidently wrong about how much leave
    somebody has.
    """
    sector, area_b = cleaning
    a_rule_set(
        sector, area_b, band=True, five="16", six="19", long_five="20", long_six="24", months=18
    )

    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name="Odd Cycle", sector=sector, sector_area=area_b
        )
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Lerato",
            last_name="Khumalo",
            date_of_birth=BORN,
            mobile_number="+27820000011",
            email="lerato@example.com",
            id_number=make_id("5013"),
        )
    a_schedule(tenant, employee, 6)
    engage(employee, start_date=ELEVEN_YEARS, job_title="Cleaner")

    with pytest.raises(cycles.EntitlementNotResolvableError) as caught:
        entitlement(employee, annual_type, AS_AT)

    message = str(caught.value)
    assert "18" in message, "name the cycle length that broke the alignment"
    assert "long-service" in message.lower()


_counter = itertools.count(1)


def a_fresh_kzn_employee(sector, area_b, *, six_day, start):
    """A brand-new tenant, employer, employee, schedule and engagement per
    Hypothesis example. A shared fixture cannot be re-engaged, and the second
    example would fail inside a broken transaction rather than on its own
    assertion."""
    n = next(_counter)
    tenant = Tenant.objects.create(trading_name=f"KZN {n}")
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name=f"KZN {n}", sector=sector, sector_area=area_b
        )
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Property",
            last_name=f"Band{n}",
            date_of_birth=BORN,
            mobile_number=f"+2783{n:07d}",
            email=f"band{n}@example.com",
            id_number=make_id(f"{6000 + n:04d}"),
        )
    # The schedule must cover the whole engagement: a cycle that opens before any
    # schedule exists reads as a five-day week, which is the engine's own answer
    # and not what this property is about.
    a_schedule(tenant, employee, 6 if six_day else 5, effective_from=start)
    engage(employee, start_date=start, job_title="Cleaner")
    return employee


# ------------------------------------------------- the band as a property


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    # Capped at 20 because the fixture employee is born in 1990 and BCEA s43(1)
    # refuses an engagement below fifteen - 21 years back from AS_AT is age 14.
    years_of_service=st.integers(min_value=1, max_value=20),
    six_day=st.booleans(),
)
def test_every_generated_cycle_reconciles_to_the_resolver_and_the_band_is_monotone(
    cleaning, leave_rules, minimum_age, annual_type, years_of_service, six_day
):
    """Two properties over any service length, because the band's arithmetic is
    the kind that reads correctly and is off by one cycle.

    **Reconciliation**: every ``entitlement_quantity`` the generator writes
    equals ``resolve.annual_leave_days()`` computed independently at that
    cycle's own last day. The engine and the resolver must not be able to
    disagree — if they can, one of them is the real rule and nobody knows which.

    **Monotonicity**: once a cycle carries the band, every later cycle carries
    it too. An employee cannot serve longer and earn less leave. This is the
    invariant an off-by-one in the boundary arithmetic breaks, and it breaks it
    in exactly one cycle out of a working life — which is why a property test
    finds it and a worked example does not.
    """
    sector, area_b = cleaning
    if not LeaveRuleSet.objects.filter(sector=sector, sector_area=area_b).exists():
        a_rule_set(sector, None, band=False, five="15", six="18")
        a_rule_set(sector, area_b, band=True, five="16", six="19", long_five="20", long_six="24")

    start = AS_AT - relativedelta(years=years_of_service)
    employee = a_fresh_kzn_employee(sector, area_b, six_day=six_day, start=start)

    made = cycles.ensure_cycles(employee, annual_type, horizon=AS_AT)
    assert made, "a generated engagement must produce at least one cycle"

    ordinary = Decimal("19") if six_day else Decimal("16")
    band = Decimal("24") if six_day else Decimal("20")

    seen_band = False
    for cycle in sorted(made, key=lambda one: one.cycle_number):
        last_day = cycle.cycle_end - datetime.timedelta(days=1)
        expected = resolve.annual_leave_days(
            sector,
            cycle.cycle_start,
            employment_start_date=start,
            six_day_week=six_day,
            sector_area=area_b,
            service_on_date=last_day,
        )
        assert cycle.entitlement_quantity == expected, (
            f"cycle {cycle.cycle_number} was written {cycle.entitlement_quantity} and the "
            f"resolver says {expected} for service to {last_day}"
        )
        assert cycle.entitlement_quantity in (ordinary, band), (
            "the band is a step, never a blend - nothing between the two figures"
        )
        if cycle.entitlement_quantity == band:
            seen_band = True
        else:
            assert not seen_band, (
                f"cycle {cycle.cycle_number} dropped back to the ordinary figure after an "
                f"earlier cycle carried the band - serving longer cannot earn less leave"
            )
