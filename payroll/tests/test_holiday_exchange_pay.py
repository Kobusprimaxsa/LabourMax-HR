"""An exchanged public holiday and a holiday worked by agreement, priced (D-319).

Two SD1 cleaning employers, the same June 2026 and the same Youth Day,
Tuesday 16 June, on the SHIPPED reference data. Each cleaner earns R40 an hour
on a nine-hour day, so a day's wage is R360.

EXCHANGED (PHA s2(2)) for Friday 19 June:
    16 June worked 9 h - an ORDINARY day now          BASIC  9 h
    17 June worked 9 h                                BASIC  9 h
    19 June not worked - the SUBSTITUTE holiday,
            paid under s18(2)(a) off the schedule     BASIC  9 h
                                    27 h x R40 = R1 080,00, and NO public holiday premium.

WORKED BY AGREEMENT (BCEA s18(1)) on 16 June:
    16 June worked 9 h - still a public holiday: s18(2)(b) "at least double",
            per DAY (D-217): max(2 x R360, R360 + 9 x R40) = R720 PH_WORKED
    17 June worked 9 h                                BASIC R360,00

Every day is captured through the grid's own save path, which reads the
employer's calendar (``leave/holidays.py``) - so the proof covers capture, the
calendar and the pricing together.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import transaction

from attendance import views
from core.managers import tenant_context
from leave.models import PublicHolidayObservance
from payroll import runs
from payroll.tests.test_termination_payslip import (
    a_cleaner,
    a_cleaning_employer,
    june,
    load_shipped,
    only_payslip,
)
from statutory.models import PublicHoliday

pytestmark = pytest.mark.django_db

YOUTH_DAY = datetime.date(2026, 6, 16)
WEDNESDAY = datetime.date(2026, 6, 17)
FRIDAY = datetime.date(2026, 6, 19)
Treatment = PublicHolidayObservance.Treatment


@pytest.fixture
def shipped(db):
    return load_shipped()


def observe(employer, day, treatment, *, name, holiday=True):
    with tenant_context(employer.tenant_id):
        return PublicHolidayObservance.objects.create(
            tenant=employer.tenant,
            employer=employer,
            public_holiday=PublicHoliday.objects.get(holiday_date=day) if holiday else None,
            observance_date=day,
            name=name,
            treatment=treatment,
        )


def capture(employee, day, typed):
    with transaction.atomic(), tenant_context(employee.tenant_id):
        row, error = views._save(employee, day, typed, replace_approved=False)
    assert error is None, error
    return row


def pay(employee, group, tax_year):
    run = runs.calculate(runs.open_run(june(group, tax_year)))
    payslip, lines = only_payslip(run)
    return payslip, {line.component_code: line for line in lines}


def test_an_exchange_prices_the_holiday_as_ordinary_and_its_substitute_as_the_holiday(shipped):
    tax_year, _ = shipped
    employer, group = a_cleaning_employer()
    cleaner, _ = a_cleaner(employer, group)
    substitute = observe(
        employer, FRIDAY, Treatment.SUBSTITUTE, name="Youth Day, exchanged", holiday=False
    )
    exchanged = observe(employer, YOUTH_DAY, Treatment.EXCHANGED, name="Youth Day")
    with tenant_context(employer.tenant_id):
        exchanged.substitute = substitute
        exchanged.save()

    assert capture(cleaner, YOUTH_DAY, "9").day_type == "ordinary"
    capture(cleaner, WEDNESDAY, "9")
    assert capture(cleaner, FRIDAY, "H").day_type == "public_holiday"

    payslip, lines = pay(cleaner, group, tax_year)

    assert "PH_WORKED" not in lines, "an exchanged day is ordinary: no s18(2)(b) premium"
    assert lines["BASIC"].units == Decimal("27.0000")
    assert lines["BASIC"].amount == Decimal("1080.00")
    assert payslip.total_earnings == Decimal("1080.00")


def test_a_holiday_worked_by_agreement_is_still_paid_at_s18_2_b(shipped):
    tax_year, _ = shipped
    employer, group = a_cleaning_employer()
    cleaner, _ = a_cleaner(employer, group)
    observe(employer, YOUTH_DAY, Treatment.WORKED_BY_AGREEMENT, name="Youth Day")

    assert capture(cleaner, YOUTH_DAY, "9").day_type == "public_holiday"
    capture(cleaner, WEDNESDAY, "9")

    payslip, lines = pay(cleaner, group, tax_year)

    assert lines["PH_WORKED"].amount == Decimal("720.00")
    assert lines["BASIC"].amount == Decimal("360.00")


def test_the_same_date_under_both_treatments_and_none(shipped):
    """Three employers, one Youth Day: exchanged is ordinary to one, a worked
    holiday to another, and the gazetted holiday to the third, which recorded
    nothing - exactly as before this change."""
    tax_year, _ = shipped
    swapped, g1 = a_cleaning_employer()
    agreed, g2 = a_cleaning_employer()
    silent, g3 = a_cleaning_employer()
    observe(swapped, YOUTH_DAY, Treatment.EXCHANGED, name="Youth Day")
    observe(agreed, YOUTH_DAY, Treatment.WORKED_BY_AGREEMENT, name="Youth Day")

    types = [
        capture(a_cleaner(employer, group)[0], YOUTH_DAY, "9").day_type
        for employer, group in ((swapped, g1), (agreed, g2), (silent, g3))
    ]

    assert types == ["ordinary", "public_holiday", "public_holiday"]


def test_an_unpaired_exchange_is_a_warning_on_the_run_never_a_block(shipped):
    """D-319, Finding 3: the software does not police the agreement, so the
    run warns and still approves on the exchange alone."""
    from payroll import validation

    tax_year, _ = shipped
    employer, group = a_cleaning_employer()
    cleaner, _ = a_cleaner(employer, group)
    observe(employer, YOUTH_DAY, Treatment.EXCHANGED, name="Youth Day")
    capture(cleaner, YOUTH_DAY, "9")

    run = runs.calculate(runs.open_run(june(group, tax_year)))
    (issue,) = [i for i in validation.validate(run) if i.issue_code == "holiday_exchange_unpaired"]

    assert issue.severity == "warning"
    assert "no substitute day is recorded" in issue.message
