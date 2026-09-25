"""Payment in lieu of the BCCCI probation band's one week (D-315, task 2).

Clause 21.2(a) prices only the one working day and the two weeks. The one week
on probation is priced by BCEA s38(1): "the remuneration the employee would have
received, calculated in accordance with section 35, if the employee had worked
during the notice period" - which ``calculators/termination.py`` applies to every
band, so it needs no figure from the agreement and does not refuse. Notice is
symmetric: a resignation and a dismissal on the same day get the same notice.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import terminate
from employees.models import EmployeeEngagement
from employers.models import Employer, PayGroup
from payroll import termination
from payroll.tests.test_termination_payslip import a_cleaner, load_shipped
from statutory.models import Sector, SectorArea

pytestmark = pytest.mark.django_db

ENGAGED = datetime.date(2026, 4, 1)
LEFT = datetime.date(2026, 5, 15)  # six weeks and a day in


@pytest.fixture
def area_b_cleaners(db):
    load_shipped()
    tenant = Tenant.objects.create(trading_name="KZN Cleaners")
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant,
            trading_name="KZN Cleaners",
            sector=Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING),
            sector_area=SectorArea.objects.get(
                sector__code=Sector.Code.CONTRACT_CLEANING, code="AREA_B"
            ),
        )
        group = PayGroup.objects.create(
            tenant=tenant,
            employer=employer,
            name="Cleaners",
            pay_frequency=PayGroup.PayFrequency.HOURLY,
            period_end_rule=PayGroup.PeriodEndRule.CALENDAR_MONTH_END,
            first_period_start=datetime.date(2026, 3, 1),
        )
    return employer, group


def leaving(employer, group, *, reason, probation_until):
    employee, engagement = a_cleaner(employer, group, start=ENGAGED)
    with tenant_context(employer.tenant_id):
        EmployeeEngagement.objects.filter(pk=engagement.pk).update(
            probation_end_date=probation_until
        )
        engagement.refresh_from_db()
    return termination.prepare(
        terminate(engagement, termination_date=LEFT, reason_code=reason, notice_worked=False)
    )


@pytest.mark.parametrize(
    "reason",
    [
        EmployeeEngagement.TerminationReason.RESIGNATION,
        EmployeeEngagement.TerminationReason.DISMISSAL_MISCONDUCT,
    ],
)
def test_on_probation_one_week_is_paid_in_lieu_under_s38(area_b_cleaners, reason):
    """R40 × 45 h = R1 800 a week; one week's notice on probation → R1 800,00."""
    employer, group = area_b_cleaners
    payout = leaving(employer, group, reason=reason, probation_until=datetime.date(2026, 7, 31))

    assert (payout.notice_weeks_required, payout.notice_pay_amount) == (
        Decimal("1.00"),
        Decimal("1800.00"),
    )


@pytest.mark.parametrize(
    "reason",
    [
        EmployeeEngagement.TerminationReason.RESIGNATION,
        EmployeeEngagement.TerminationReason.DISMISSAL_MISCONDUCT,
    ],
)
def test_off_probation_two_weeks_is_paid_in_lieu(area_b_cleaners, reason):
    """No probation captured means not on probation (employees/probation.py):
    two weeks → R3 600,00, clause 21.2(a)(ii)'s double the weekly wage."""
    employer, group = area_b_cleaners
    payout = leaving(employer, group, reason=reason, probation_until=None)

    assert (payout.notice_weeks_required, payout.notice_pay_amount) == (
        Decimal("2.00"),
        Decimal("3600.00"),
    )
