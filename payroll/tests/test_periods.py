"""The pay period generator — P3's definition of done, and the ways a calendar lies.

An employer sets a calendar once and a year of periods falls out. The interesting
tests are not that twelve months make twelve periods; they are the boundaries where a
plausible calendar produces a wrong answer that nothing downstream would catch:

- a week that ends in February and pays in March belongs to the NEW tax year,
- a period that ends on the 29th of February in a year that has no 29th,
- an anchor date before the tax year, which must not produce periods that pay before
  the year began,
- and re-running generation, which must not renumber periods a run may have touched.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import Tenant
from employers.models import Employer, PayGroup
from payroll.models import PayPeriod
from payroll.periods import (
    PeriodGenerationError,
    generate_for_tax_year,
    periods_for,
    working_days_between,
)
from statutory.models import Sector, TaxYear

MARCH_2026 = datetime.date(2026, 3, 1)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="A subscriber")


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def employer(db, tenant, sector):
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="An Employer", sector=sector)


@pytest.fixture
def tax_year(db):
    return TaxYear.objects.create(
        label="2026/2027", start_date=MARCH_2026, end_date=datetime.date(2027, 2, 28)
    )


def pay_group(tenant, employer, **overrides):
    values = {
        "name": "Staff",
        "pay_frequency": PayGroup.PayFrequency.MONTHLY,
        "period_end_rule": PayGroup.PeriodEndRule.CALENDAR_MONTH_END,
        "first_period_start": MARCH_2026,
        **overrides,
    }
    with tenant_context(tenant.pk):
        return PayGroup.objects.create(tenant=tenant, employer=employer, **values)


# ------------------------------------------------------------------- the shapes


@pytest.mark.django_db
def test_a_calendar_month_group_gets_twelve_periods(tenant, employer, tax_year):
    group = pay_group(tenant, employer)
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    assert len(created) == 12
    assert created[0].period_start == MARCH_2026
    assert created[0].period_end == datetime.date(2026, 3, 31)
    assert created[-1].period_end == datetime.date(2027, 2, 28)
    assert [p.period_number for p in created] == list(range(1, 13))


@pytest.mark.django_db
def test_february_needs_no_special_case_because_the_day_is_capped_at_28(tenant, employer, tax_year):
    """A fixed day of 28 lands on the 28th in every month, leap year or not."""
    group = pay_group(
        tenant,
        employer,
        period_end_rule=PayGroup.PeriodEndRule.FIXED_DAY_OF_MONTH,
        period_end_day_of_month=28,
    )
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    assert all(period.period_end.day == 28 for period in created)


@pytest.mark.django_db
def test_a_weekly_group_gets_fifty_two_or_fifty_three_periods(tenant, employer, tax_year):
    group = pay_group(
        tenant,
        employer,
        pay_frequency=PayGroup.PayFrequency.WEEKLY,
        period_end_rule=PayGroup.PeriodEndRule.WEEK_ENDING_DAY,
        week_ending_weekday=4,  # Friday
    )
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    assert 52 <= len(created) <= 53
    assert all(period.period_end.weekday() == 4 for period in created)


@pytest.mark.django_db
def test_a_fortnightly_group_pays_every_fourteen_days(tenant, employer, tax_year):
    group = pay_group(
        tenant,
        employer,
        pay_frequency=PayGroup.PayFrequency.FORTNIGHTLY,
        period_end_rule=PayGroup.PeriodEndRule.WEEK_ENDING_DAY,
        week_ending_weekday=4,
    )
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    gaps = {
        (later.period_end - earlier.period_end).days
        for earlier, later in zip(created, created[1:], strict=False)
    }
    assert gaps == {14}


@pytest.mark.django_db
def test_an_hourly_group_takes_its_cadence_from_the_period_rule(tenant, employer, tax_year):
    """'Hourly' says how pay is computed, not how often it is paid. An hourly cleaner
    paid every Friday is on a weekly cadence, and the workbook gives no other way to
    say so."""
    group = pay_group(
        tenant,
        employer,
        pay_frequency=PayGroup.PayFrequency.HOURLY,
        period_end_rule=PayGroup.PeriodEndRule.WEEK_ENDING_DAY,
        week_ending_weekday=4,
    )
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    assert len(created) >= 52
    assert group.is_attendance_driven is True


# --------------------------------------------------------- the tax year boundary


@pytest.mark.django_db
def test_a_period_belongs_to_the_tax_year_its_payment_falls_in(tenant, employer, tax_year):
    """THE ONE THAT MOVES MONEY ONTO THE WRONG IRP5.

    A weekly period ending 27 February pays on 3 March with a four-day offset. Those
    earnings are in the NEW tax year. Assigning by period end would put a week of pay
    on the wrong certificate, and every payslip would look correct.
    """
    previous_year = TaxYear.objects.create(
        label="2025/2026",
        start_date=datetime.date(2025, 3, 1),
        end_date=datetime.date(2026, 2, 28),
    )
    group = pay_group(
        tenant,
        employer,
        pay_frequency=PayGroup.PayFrequency.WEEKLY,
        period_end_rule=PayGroup.PeriodEndRule.WEEK_ENDING_DAY,
        week_ending_weekday=4,
        payment_day_offset=4,
        first_period_start=datetime.date(2026, 2, 21),
    )

    computed = periods_for(group, previous_year)
    ending_in_february = [p for p in computed if p.end.month == 2]
    assert all(p.payment_date <= previous_year.end_date for p in ending_in_february), (
        "A period paying in March must not be counted into the year that ended in February."
    )

    with tenant_context(tenant.pk):
        into_new_year = generate_for_tax_year(group, tax_year)
    assert into_new_year[0].payment_date >= MARCH_2026


@pytest.mark.django_db
def test_periods_paying_before_the_tax_year_started_are_not_generated(tenant, employer, tax_year):
    """The anchor is two years early. Walking forward from it must still produce only
    the year asked for."""
    group = pay_group(tenant, employer, first_period_start=datetime.date(2024, 3, 1))
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    assert len(created) == 12
    assert all(MARCH_2026 <= period.payment_date <= tax_year.end_date for period in created)


@pytest.mark.django_db
def test_the_payment_offset_moves_the_payment_date_and_nothing_else(tenant, employer, tax_year):
    group = pay_group(tenant, employer, payment_day_offset=3)
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    first = created[0]
    assert first.period_end == datetime.date(2026, 3, 31)
    assert first.payment_date == datetime.date(2026, 4, 3)


# ------------------------------------------------------------------- refusals


@pytest.mark.django_db
def test_generating_without_a_tax_year_is_refused(tenant, employer):
    """A pay period with no tax year cannot be reconciled to an IRP5, and the
    reference data is exactly where the answer should come from."""
    from payroll.periods import generate_for_date

    group = pay_group(tenant, employer)
    with pytest.raises(PeriodGenerationError) as caught, tenant_context(tenant.pk):
        generate_for_date(group, MARCH_2026)

    assert "Load the statutory reference data" in str(caught.value)


@pytest.mark.django_db
def test_an_anchor_that_never_reaches_the_tax_year_is_refused(tenant, employer, tax_year):
    """Walking forward from a mistyped year must stop and say so, not spin."""
    group = pay_group(
        tenant,
        employer,
        pay_frequency=PayGroup.PayFrequency.WEEKLY,
        period_end_rule=PayGroup.PeriodEndRule.WEEK_ENDING_DAY,
        week_ending_weekday=4,
        first_period_start=datetime.date(1926, 3, 1),
    )
    with pytest.raises(PeriodGenerationError) as caught:
        periods_for(group, tax_year)

    assert "anchor date" in str(caught.value)


@pytest.mark.django_db
def test_a_calendar_that_pays_nothing_in_the_year_is_refused(tenant, employer, tax_year):
    group = pay_group(tenant, employer, first_period_start=datetime.date(2030, 3, 1))
    with pytest.raises(PeriodGenerationError) as caught, tenant_context(tenant.pk):
        generate_for_tax_year(group, tax_year)

    assert "no period of this calendar pays inside" in str(caught.value)


# ---------------------------------------------------------------- re-running


@pytest.mark.django_db
def test_generating_twice_creates_nothing_and_renumbers_nothing(tenant, employer, tax_year):
    """A payroll run may already have touched these periods."""
    group = pay_group(tenant, employer)
    with tenant_context(tenant.pk):
        first = generate_for_tax_year(group, tax_year)
        second = generate_for_tax_year(group, tax_year)

        assert second == []
        assert PayPeriod.objects.filter(pay_group=group).count() == len(first)
        assert [p.period_number for p in PayPeriod.objects.filter(pay_group=group)] == list(
            range(1, 13)
        )


# ------------------------------------------------------------- working days


@pytest.mark.django_db
def test_a_five_day_week_counts_monday_to_friday():
    """March 2026 has 22 weekdays."""
    assert working_days_between(
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31), Decimal(5)
    ) == Decimal(22)


@pytest.mark.django_db
def test_a_six_day_week_includes_saturday():
    """March 2026 opens on a Sunday, so it holds five Sundays: 31 days less those
    five is 26 working days on a six-day week, against 22 on a five-day one."""
    assert working_days_between(
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31), Decimal(6)
    ) == Decimal(26)


@pytest.mark.django_db
def test_a_fractional_week_is_pro_rated_rather_than_given_a_shape():
    """A four-and-a-half day week has no fixed weekday pattern, and inventing one
    would put a day in the answer the employer never named."""
    result = working_days_between(
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31), Decimal("4.5")
    )
    assert Decimal(19) < result < Decimal(21)


@pytest.mark.django_db
def test_public_holidays_are_not_subtracted(tenant, employer, tax_year):
    """A public holiday on an ordinary working day is PAID, so it stays an available
    day. Subtracting it would under-count the month for a salaried employee."""
    group = pay_group(tenant, employer)
    with tenant_context(tenant.pk):
        created = generate_for_tax_year(group, tax_year)

    april = next(p for p in created if p.period_start.month == 4)
    # April 2026 has 22 weekdays, two of which are public holidays.
    assert april.working_days_in_period == Decimal("22.000")
