"""Family responsibility leave beyond the statute (D-318), and the refusal that
stays the default.

BCEA s27(1) is a FLOOR. An employer may give more - leave to a three-day-a-week
domestic worker, or more than the statutory days to somebody the Act covers -
and it is recorded as CONTRACTUAL: its own leave type, its own cycle and
ledger, granted with a named person and a reason, paid or unpaid as the
employer chose. The statutory entitlement is never touched.

Every refusal is asserted on its message (D-134). The rule set fixture is the
BCEA shape (three days, four months, four days a week); SD7's five days are the
same mechanism with a different figure.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.managers import platform_context, tenant_context, tenant_context_of
from core.models import AppUser, TenantMembership
from employees.models import EmployeeLeaveEntitlement
from leave import contractual
from leave.accrual import FAMILY_RESPONSIBILITY_BASIS, accrue_employee
from leave.applications import (
    ApplicationRefusedError,
    FamilyResponsibilityIneligibleError,
    submit_application,
)
from leave.authorisation import approve
from leave.contractual import BeyondStatute, GrantRefusedError
from leave.models import LeaveApplication, LeaveCycle, LeaveTransaction, LeaveType
from leave.tests.conftest import START
from leave.tests.test_family_responsibility_eligibility import TWO_YEARS_IN, _schedule
from leave.types import seed_system_leave_types

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(tenant):
    user = AppUser.objects.create_user(
        email="owner@example.com", password="x" * 16, first_name="Kobus", last_name="Owner"
    )
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(tenant=tenant, user=user, role="owner")
    return user


@pytest.fixture
def contractual_type(db):
    seed_system_leave_types()
    with platform_context():
        return LeaveType.objects.get(
            code=LeaveType.Code.FAMILY_RESPONSIBILITY_CONTRACTUAL, tenant__isnull=True
        )


def a_grant(employee, owner, *, days="3", paid=True, effective_from=TWO_YEARS_IN):
    """Made in the cycle the leave is taken in, so the grant itself posts that
    cycle's days; later cycles are the accrual engine's (tested below)."""
    return contractual.grant(
        employee,
        days_per_cycle=Decimal(days),
        is_paid=paid,
        granted_by=owner,
        reason="Household policy: our only employee gets family leave regardless of days.",
        effective_from=effective_from,
    )


def cycles_of(employee, leave_type):
    with tenant_context_of(employee):
        return list(LeaveCycle.objects.filter(employee=employee, leave_type=leave_type))


def ledger(employee, leave_type):
    with tenant_context_of(employee):
        return list(
            LeaveTransaction.objects.filter(employee=employee, leave_type=leave_type).order_by("pk")
        )


def authorised(owner):
    return BeyondStatute(authorised_by=owner, reason="A death in the family; our policy covers it.")


# ================================================ the refusal is still the default


def test_three_days_a_week_is_still_refused_by_default_and_says_the_law_does_not_require_it(
    employee, engagement, leave_rules, family_type, contractual_type, owner
):
    _schedule(employee, 3)
    a_grant(employee, owner)  # even a grant in place does not bypass the default

    with pytest.raises(FamilyResponsibilityIneligibleError) as raised:
        submit_application(
            employee, leave_type=family_type, start_date=TWO_YEARS_IN, end_date=TWO_YEARS_IN
        )

    message = str(raised.value)
    assert "works 3 days a week, and BCEA s27(1)(b) requires at least 4" in message
    assert "The law does not require this leave" in message
    assert "naming who authorised it and why" in message
    with tenant_context_of(employee):
        assert not LeaveApplication.objects.filter(employee=employee).exists()


def test_an_authoriser_with_no_reason_is_refused(
    employee, engagement, leave_rules, family_type, contractual_type, owner
):
    _schedule(employee, 3)
    a_grant(employee, owner)

    with pytest.raises(
        ApplicationRefusedError, match="needs the person authorising it and a reason"
    ):
        submit_application(
            employee,
            leave_type=family_type,
            start_date=TWO_YEARS_IN,
            end_date=TWO_YEARS_IN,
            beyond_statute=BeyondStatute(authorised_by=owner, reason="  "),
        )


def test_authorised_but_with_no_grant_is_refused_because_the_days_come_from_a_grant(
    employee, engagement, leave_rules, family_type, contractual_type, owner
):
    _schedule(employee, 3)

    with pytest.raises(ApplicationRefusedError, match="No contractual family responsibility grant"):
        submit_application(
            employee,
            leave_type=family_type,
            start_date=TWO_YEARS_IN,
            end_date=TWO_YEARS_IN,
            beyond_statute=authorised(owner),
        )


# ============================================================ the override


def test_authorised_leave_is_granted_as_contractual_and_the_statute_is_untouched(
    employee, engagement, leave_rules, family_type, contractual_type, owner, working_time_rules
):
    """Three days a week, two years in, a paid three-day grant. Two days off on
    authority: recorded against the CONTRACTUAL type, both people named, paid,
    and there is no statutory family responsibility cycle or ledger row at all."""
    _schedule(employee, 3)
    a_grant(employee, owner, days="3", paid=True)
    tuesday = TWO_YEARS_IN + datetime.timedelta(days=1)

    application = submit_application(
        employee,
        leave_type=family_type,
        start_date=TWO_YEARS_IN,
        end_date=tuesday,
        beyond_statute=authorised(owner),
    )
    approve(application, decided_by=owner, self_approval_reason="Sole owner")

    assert application.leave_type == contractual_type
    assert application.leave_type.is_statutory is False
    assert application.beyond_statute_authorised_by == owner
    assert application.beyond_statute_reason == "A death in the family; our policy covers it."
    with tenant_context_of(employee):
        days = list(application.days.filter(is_working_day=True))
    assert [(d.is_paid, d.deducted_from_balance) for d in days] == [(True, True), (True, True)]

    rows = ledger(employee, contractual_type)
    assert [(r.transaction_type, r.days, r.calculation_basis) for r in rows] == [
        ("accrual", Decimal("3.000"), contractual.CONTRACTUAL_BASIS),
        ("taken", Decimal("-2.000"), "manual"),
    ]
    assert rows[0].reason.startswith("Household policy")
    assert cycles_of(employee, family_type) == [], "no statutory cycle for somebody s27 excludes"
    assert ledger(employee, family_type) == []


def test_an_unpaid_grant_gives_the_days_off_unpaid_and_still_spends_them(
    employee, engagement, leave_rules, family_type, contractual_type, owner
):
    _schedule(employee, 3)
    a_grant(employee, owner, days="3", paid=False)

    application = submit_application(
        employee,
        leave_type=family_type,
        start_date=TWO_YEARS_IN,
        end_date=TWO_YEARS_IN,
        beyond_statute=authorised(owner),
    )

    with tenant_context_of(employee):
        (day,) = application.days.filter(is_working_day=True)
    assert (day.is_paid, day.deducted_from_balance) == (False, True)
    assert application.unpaid_days == Decimal("1.000")


# ================================ an eligible employee: statutory days unchanged


def test_an_eligible_employee_keeps_the_statutory_days_and_gets_more_by_grant(
    employee,
    engagement,
    leave_rules,
    family_type,
    contractual_type,
    owner,
    schedule_5day,
    working_time_rules,
):
    """NO REGRESSION: five days a week, two years in, the statutory accrual posts
    the rule set's figure exactly as before. A contractual grant of two more
    sits in its own cycle, and a day taken on the contractual type spends the
    contractual balance and not the statutory one."""
    with tenant_context_of(employee):
        statutory = accrue_employee(employee, family_type, as_at=TWO_YEARS_IN)
    assert statutory.calculation_basis == FAMILY_RESPONSIBILITY_BASIS
    assert statutory.days == Decimal(leave_rules.family_responsibility_days)

    a_grant(employee, owner, days="2")
    application = submit_application(
        employee, leave_type=contractual_type, start_date=TWO_YEARS_IN, end_date=TWO_YEARS_IN
    )
    approve(application, decided_by=owner, self_approval_reason="Sole owner")

    from leave.balances import balance_as_at

    with tenant_context_of(employee):
        assert balance_as_at(employee, family_type, TWO_YEARS_IN).balance_quantity == Decimal(
            leave_rules.family_responsibility_days
        )
        assert balance_as_at(employee, contractual_type, TWO_YEARS_IN).balance_quantity == (
            Decimal("1.000")
        )
    assert application.beyond_statute_authorised_by is None


def test_an_eligible_employee_cannot_be_authorised_beyond_statute_on_the_statutory_type(
    employee, engagement, leave_rules, family_type, contractual_type, owner, schedule_5day
):
    with pytest.raises(ApplicationRefusedError, match="IS covered by BCEA s27\\(1\\)"):
        submit_application(
            employee,
            leave_type=family_type,
            start_date=TWO_YEARS_IN,
            end_date=TWO_YEARS_IN,
            beyond_statute=authorised(owner),
        )


def test_the_statutory_accrual_still_grants_nothing_to_somebody_s27_excludes(
    employee, engagement, leave_rules, family_type, contractual_type, owner
):
    """leave/accrual.py's statutory path does not change: a grant elsewhere
    does not make a three-day-a-week employee eligible."""
    _schedule(employee, 3)
    a_grant(employee, owner)
    with tenant_context_of(employee):
        assert accrue_employee(employee, family_type, as_at=TWO_YEARS_IN) is None
    assert ledger(employee, family_type) == []


# ================================================================ the grant


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"granted_by": None}, "names the person who granted it"),
        ({"reason": "   "}, "states why"),
        ({"days_per_cycle": Decimal("0")}, "grants nothing"),
    ],
)
def test_a_grant_missing_what_makes_it_a_grant_is_refused(
    employee, engagement, leave_rules, contractual_type, owner, overrides, message
):
    values = {
        "days_per_cycle": Decimal("3"),
        "is_paid": True,
        "granted_by": owner,
        "reason": "Policy",
        "effective_from": START,
    } | overrides
    with pytest.raises(GrantRefusedError, match=message):
        contractual.grant(employee, **values)


def test_extra_days_on_the_statutory_type_are_refused_and_pointed_at_the_grant(
    employee, engagement, family_type
):
    """The mixing this decision exists to prevent: additional days on the
    STATUTORY type would sit in the statutory cycle."""
    with tenant_context_of(employee):
        row = EmployeeLeaveEntitlement(
            tenant=employee.tenant,
            employee=employee,
            leave_type=family_type,
            additional_days_per_cycle=Decimal("2"),
            effective_from=START,
        )
        with pytest.raises(ValidationError, match="grant them on the contractual type"):
            row.full_clean()


# ======================================================== the two CHECKs, refusing


def test_an_application_authorised_without_a_reason_is_refused_by_the_database(
    employee, engagement, leave_rules, family_type, contractual_type, owner
):
    _schedule(employee, 3)
    a_grant(employee, owner)
    application = submit_application(
        employee,
        leave_type=family_type,
        start_date=TWO_YEARS_IN,
        end_date=TWO_YEARS_IN,
        beyond_statute=authorised(owner),
    )
    with (
        pytest.raises(
            IntegrityError, match="leave_application_beyond_statute_is_named_and_reasoned"
        ),
        transaction.atomic(),
        tenant_context_of(employee),
    ):
        LeaveApplication.objects.filter(pk=application.pk).update(beyond_statute_reason="")


def test_a_grant_with_a_grantor_and_no_reason_is_refused_by_the_database(
    employee, engagement, leave_rules, contractual_type, owner
):
    row = a_grant(employee, owner)
    with (
        pytest.raises(IntegrityError, match="entitlement_grant_is_named_and_reasoned"),
        transaction.atomic(),
        tenant_context_of(employee),
    ):
        EmployeeLeaveEntitlement.objects.filter(pk=row.pk).update(grant_reason="")


def test_the_accrual_engine_posts_each_later_cycles_grant_upfront(
    employee, engagement, leave_rules, contractual_type, owner
):
    """A grant made in March 2026 is posted then; the next cycle's days come
    from the engine, once, on the cycle's first day - never twice."""
    _schedule(employee, 3)
    a_grant(employee, owner, days="3", effective_from=START)
    next_cycle = datetime.date(2027, 3, 1)

    with tenant_context_of(employee):
        posted = accrue_employee(employee, contractual_type, as_at=next_cycle)
        again = accrue_employee(employee, contractual_type, as_at=next_cycle)

    assert (posted.days, posted.transaction_date) == (Decimal("3.000"), next_cycle)
    assert posted.calculation_basis == contractual.CONTRACTUAL_BASIS
    assert again is None
