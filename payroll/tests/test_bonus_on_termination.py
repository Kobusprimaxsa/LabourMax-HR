"""O-44 closed (D-320): the annual bonus on termination, at the edges.

A leaver is paid PRO RATA on the COMPLETED FULL CALENDAR MONTHS of the CURRENT
cycle, however long their service, in both contract cleaning instruments - SD1
clause 3(3) at 4,333 weeks, the BCCCI's clause 4.5 at 4,33. The domestic sector
and the BCEA give no bonus at all. Every figure is written out; the weekly wage
is SD1 Area A's R1 497,15 throughout, so only the multiplier and the months move.
"""

from __future__ import annotations

import datetime
import types
from decimal import Decimal

import pytest

from calculators.base import Money
from calculators.bonus import BonusInputError, annual_bonus
from calculators.tests.test_bonus import NO_BONUS, a_bonus
from core.managers import tenant_context
from employees.engagements import terminate
from employees.models import EmployeeEngagement
from payroll import bonus
from payroll.bonusrun import owed_in
from payroll.tests.test_bonus import (
    SD1_WEEKLY,
    an_employee,
    an_employer,
    elect,
    load_bonus_reference,
    rows_of,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def reference(db):
    return load_bonus_reference()


FOUR_YEARS_AGO = datetime.date(2022, 6, 1)
DECEMBER = types.SimpleNamespace(
    period_start=datetime.date(2026, 12, 1), period_end=datetime.date(2026, 12, 31)
)


def a_leaver(reference, instrument, *, start, leaving):
    """A cleaner under SD1 (Areas A and C) or the BCCCI (Area B)."""
    area = reference["area_b"] if instrument == "bccci" else None
    employer, pay_group = an_employer(reference["cleaning"], area)
    employee = an_employee(employer, pay_group, start=start)
    with tenant_context(employer.tenant_id):
        engagement = EmployeeEngagement.objects.get(employee=employee)
    terminate(
        engagement,
        termination_date=leaving,
        reason_code=EmployeeEngagement.TerminationReason.RESIGNATION,
        notice_worked=True,
    )
    return employer, employee


def paid_on_leaving(employee, leaving):
    return annual_bonus(bonus.termination_bonus(employee, leaving))


@pytest.mark.parametrize(
    "instrument, weeks",
    [("sd1", Decimal("4.333")), ("bccci", Decimal("4.330"))],
)
def test_four_years_service_leaving_12_june_is_five_twelfths_not_the_whole(
    reference, instrument, weeks
):
    """January to May: 5 full months. 12 June is not a full month.
    SD1:   5/12 x 4,333 x R1 497,15 = R2 702,98
    BCCCI: 5/12 x 4,33  x R1 497,15 = R2 701,11
    The whole bonus would be 12/12 - R6 487,15 and R6 482,66."""
    leaving = datetime.date(2026, 6, 12)
    _, employee = a_leaver(reference, instrument, start=FOUR_YEARS_AGO, leaving=leaving)

    result = paid_on_leaving(employee, leaving)

    assert result.full_months == 5
    assert result.amount == Money.of(weeks * SD1_WEEKLY * 5 / 12)
    assert (
        result.amount.rounded
        == {"sd1": Decimal("2702.98"), "bccci": Decimal("2701.11")}[instrument]
    )
    assert result.amount.rounded < Money.of(weeks * SD1_WEEKLY).rounded


@pytest.mark.parametrize("instrument", ["sd1", "bccci"])
def test_in_employment_on_1_december_and_leaving_on_the_10th_is_eleven_twelfths_and_paid_once(
    reference, instrument
):
    """January to November: 11 full months; December is not complete. The
    December bonus run does NOT also pay them - owed_in() leaves a leaver out,
    their share having been paid on the termination payslip."""
    leaving = datetime.date(2026, 12, 10)
    _, employee = a_leaver(reference, instrument, start=FOUR_YEARS_AGO, leaving=leaving)

    assert paid_on_leaving(employee, leaving).full_months == 11
    assert owed_in(DECEMBER, employee) is False


def test_joining_after_the_first_earns_nothing_for_that_month_by_default(reference):
    """Joined 5 March, left 12 June: April and May only - 2/12. March is not a
    full calendar month of service (BCCCI 4.5(c)(ii); SD1 'full calendar
    months')."""
    leaving = datetime.date(2026, 6, 12)
    for instrument in ("sd1", "bccci"):
        _, employee = a_leaver(
            reference, instrument, start=datetime.date(2026, 3, 5), leaving=leaving
        )
        assert paid_on_leaving(employee, leaving).full_months == 2, instrument


def test_a_kzn_employer_may_elect_that_the_part_first_month_counts(reference):
    """4.5(g) makes 4.5(c)(ii) a minimum the employer may improve on (D-242):
    elected, March counts and the leaver is paid 3/12. SD1 grants no such
    discretion, so the same election does not move an SD1 cleaner."""
    leaving = datetime.date(2026, 6, 12)
    kzn, kzn_cleaner = a_leaver(
        reference, "bccci", start=datetime.date(2026, 3, 5), leaving=leaving
    )
    sd1, sd1_cleaner = a_leaver(reference, "sd1", start=datetime.date(2026, 3, 5), leaving=leaving)
    elect(kzn, "BONUS_PART_MONTH_EARNS_NOTHING", False)
    elect(sd1, "BONUS_PART_MONTH_EARNS_NOTHING", False)

    assert paid_on_leaving(kzn_cleaner, leaving).full_months == 3
    assert paid_on_leaving(sd1_cleaner, leaving).full_months == 2


def test_a_domestic_employer_has_no_bonus_rows_and_the_calculator_refuses_a_zero(reference):
    employer, pay_group = an_employer(reference["domestic"], name="Household")
    employee = an_employee(employer, pay_group, start=FOUR_YEARS_AGO, weekly=Decimal("1100.00"))

    assert bonus.termination_bonus(employee, datetime.date(2026, 6, 12)) is None
    assert bonus.accrue(employer, as_at=datetime.date(2026, 6, 30)) == []
    assert rows_of(employer) == []
    with pytest.raises(BonusInputError, match="gives no annual bonus"):
        annual_bonus(a_bonus(rule=NO_BONUS))


def test_the_two_multipliers_are_pinned_apart_by_sector_and_area(reference):
    """4,33 and 4,333 look almost identical and are different figures in
    different instruments. The BCCCI's is the AREA-scoped row."""
    from statutory import resolve

    cleaning, area_b = reference["cleaning"], reference["area_b"]
    on = datetime.date(2026, 6, 30)
    sd1 = resolve.termination_rules(cleaning, on)
    bccci = resolve.termination_rules(cleaning, on, area_b)

    assert (sd1.annual_bonus_weeks, sd1.sector_area_id) == (Decimal("4.333"), None)
    assert (bccci.annual_bonus_weeks, bccci.sector_area_id) == (Decimal("4.330"), area_b.pk)
    assert bccci.annual_bonus_pro_rata_on_termination is True, (
        "Kobus's election (D-320): a KZN leaver is paid pro rata although clause 4.5 "
        "has no termination limb"
    )
