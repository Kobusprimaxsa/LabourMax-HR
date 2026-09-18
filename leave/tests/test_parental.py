"""Parental leave under Van Wyk's interim reading-in (D-201 to D-205).

The order makes s25 one parent-neutral entitlement with TWO totals: four
consecutive months for a single parent or the only employed party (read-in
s25(1)), and four months and ten days IN THE AGGREGATE where both parties are
employed (read-in s25(4A)). Which one applies turns on whether the other parent
is employed — a fact about somebody who is not this employer's employee — so it
is DECLARED, never computed.

What this software checks is only what it can: a share above the declared
shape's maximum (refused), and the single sequence s25(4B) requires (enforced).
What it does not check it does not pretend to: a share BELOW the maximum is
accepted without comment, because an employee may take less leave than they are
entitled to.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from core.managers import tenant_context
from core.models import AppUser, TenantMembership
from leave.applications import ApplicationRefusedError, submit_application
from leave.models import LeaveApplication, LeaveType
from leave.parental import ParentalDeclaration, RelationshipShape

pytestmark = pytest.mark.django_db

BIRTH = datetime.date(2026, 3, 16)  # a Monday
AFTER_LAPSE = datetime.date(2028, 11, 6)  # a Monday, past the 3 October 2028 expiry


@pytest.fixture
def owner(tenant):
    user = AppUser.objects.create_user(email="parental-owner@example.com", password="x" * 16)
    with tenant_context(tenant.pk):
        TenantMembership.objects.create(tenant=tenant, user=user, role=TenantMembership.Role.OWNER)
    return user


@pytest.fixture
def parental_quantum(db):
    """The interim quantum, as the fixture loads it: four months, or four months
    and ten days in the aggregate, ending with the suspension."""
    from statutory.models import ParentalLeaveQuantum

    return ParentalLeaveQuantum.objects.create(
        effective_from=datetime.date(2025, 10, 3),
        effective_to=datetime.date(2028, 10, 3),
        sole_parent_months=4,
        sole_parent_days=0,
        both_employed_months=4,
        both_employed_days=10,
        source_reference="Van Wyk (CCT 308/23) [2025] ZACC 20, order para 5(a)",
    )


@pytest.fixture
def adoption_limits(db):
    from statutory.models import AdoptionAgeLimit

    AdoptionAgeLimit.objects.create(
        effective_from=datetime.date(2025, 10, 3),
        effective_to=datetime.date(2028, 10, 3),
        is_limited=True,
        max_child_age_years=2,
        source_reference="Van Wyk, order paras 3, 4 and 5(c)",
    )
    AdoptionAgeLimit.objects.create(
        effective_from=datetime.date(2028, 10, 3),
        is_limited=False,
        source_reference="Van Wyk, order paras 3 and 4",
    )


@pytest.fixture
def parental_type(db):
    from leave.types import seed_system_leave_types

    seed_system_leave_types()
    from core.managers import platform_context

    with platform_context():
        return LeaveType.objects.get(code=LeaveType.Code.PARENTAL, tenant__isnull=True)


@pytest.fixture
def maternity_type(parental_type):
    from core.managers import platform_context

    with platform_context():
        return LeaveType.objects.get(code=LeaveType.Code.MATERNITY, tenant__isnull=True)


@pytest.fixture
def adoption_type(parental_type):
    from core.managers import platform_context

    with platform_context():
        return LeaveType.objects.get(code=LeaveType.Code.ADOPTION, tenant__isnull=True)


def declaration(**overrides):
    values = {
        "shape": RelationshipShape.BOTH_EMPLOYED,
        "share_months": 2,
        "share_days": 0,
        "event_date": BIRTH,
    }
    values.update(overrides)
    return ParentalDeclaration(**values)


def apply_for(employee, leave_type, owner, *, start, end, declared=None, **kwargs):
    return submit_application(
        employee,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        parental=declared if declared is not None else declaration(),
        submitted_by=owner,
        **kwargs,
    )


# ------------------------------------------------------------- the two totals


def test_the_two_totals_come_from_reference_data_not_from_code(parental_quantum):
    """No literal four, ten or half anywhere in this chunk."""
    from statutory import resolve

    quantum = resolve.parental_quantum(BIRTH)
    assert (quantum.sole_parent_months, quantum.sole_parent_days) == (4, 0)
    assert (quantum.both_employed_months, quantum.both_employed_days) == (4, 10)


@pytest.mark.parametrize(
    ("shape", "months", "days", "ceiling"),
    [
        (RelationshipShape.SINGLE_PARENT, 4, 1, "4 months"),
        (RelationshipShape.ONLY_EMPLOYED_PARTY, 5, 0, "4 months"),
        (RelationshipShape.BOTH_EMPLOYED, 4, 11, "4 months and 10 days"),
    ],
)
def test_a_share_above_the_declared_shapes_maximum_is_refused(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
    shape,
    months,
    days,
    ceiling,
):
    with pytest.raises(ApplicationRefusedError) as raised:
        apply_for(
            employee,
            parental_type,
            owner,
            start=BIRTH,
            end=BIRTH,
            declared=declaration(shape=shape, share_months=months, share_days=days),
        )

    message = str(raised.value)
    assert ceiling in message, message
    assert shape.label.lower() in message.lower() or shape.value.lower() in message.lower(), message
    with tenant_context(employee.tenant_id):
        assert not LeaveApplication.objects.filter(employee=employee).exists()


def test_a_share_below_the_maximum_is_accepted_without_complaint(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    """An employee may take less leave than they are entitled to. A system that
    refuses a mother who wants three months back at work is wrong in a way she
    will remember."""
    application = apply_for(
        employee,
        parental_type,
        owner,
        start=BIRTH,
        end=BIRTH + datetime.timedelta(days=13),
        declared=declaration(shape=RelationshipShape.SINGLE_PARENT, share_months=3, share_days=0),
    )
    assert application.status == LeaveApplication.Status.SUBMITTED
    assert application.parental_share_months == 3


def test_the_declaration_is_recorded_with_who_declared_it_and_when(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    """The employer's defence, if this is ever disputed, is the record."""
    application = apply_for(employee, parental_type, owner, start=BIRTH, end=BIRTH)

    assert application.parental_relationship_shape == RelationshipShape.BOTH_EMPLOYED
    assert application.parental_event_date == BIRTH
    assert application.parental_declared_by_user_id == owner.pk
    assert application.parental_declared_at is not None


def test_an_application_with_no_declaration_is_refused_naming_what_is_missing(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    with pytest.raises(ApplicationRefusedError) as raised:
        submit_application(
            employee,
            leave_type=parental_type,
            start_date=BIRTH,
            end_date=BIRTH,
            submitted_by=owner,
        )
    message = str(raised.value)
    assert "declaration" in message.lower(), message
    assert "s25(4A)" in message or "relationship" in message.lower(), message


# ------------------------------------------------- the single sequence, s25(4B)


def test_a_detached_second_period_for_the_same_event_is_refused(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    """Two months off, back to work, two months off again is not what the order
    permits, and the employer who allowed it carries that."""
    first = apply_for(
        employee, parental_type, owner, start=BIRTH, end=BIRTH + datetime.timedelta(days=20)
    )

    with pytest.raises(ApplicationRefusedError) as raised:
        apply_for(
            employee,
            parental_type,
            owner,
            start=BIRTH + datetime.timedelta(days=40),
            end=BIRTH + datetime.timedelta(days=50),
        )

    message = str(raised.value)
    assert "s25(4B)" in message, message
    assert f"{first.start_date:%d %B %Y}" in message, message
    assert f"{first.end_date:%d %B %Y}" in message, message


def test_a_contiguous_extension_is_the_same_sequence_and_is_allowed(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    """Extending the sequence the day after it ends is one sequence, not two."""
    first = apply_for(
        employee, parental_type, owner, start=BIRTH, end=BIRTH + datetime.timedelta(days=20)
    )
    second = apply_for(
        employee,
        parental_type,
        owner,
        start=first.end_date + datetime.timedelta(days=1),
        end=first.end_date + datetime.timedelta(days=15),
    )
    assert second.status == LeaveApplication.Status.SUBMITTED


def test_a_different_event_starts_its_own_sequence(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    apply_for(employee, parental_type, owner, start=BIRTH, end=BIRTH + datetime.timedelta(days=20))
    later_birth = BIRTH + datetime.timedelta(days=400)
    second = apply_for(
        employee,
        parental_type,
        owner,
        start=later_birth,
        end=later_birth + datetime.timedelta(days=10),
        declared=declaration(event_date=later_birth),
    )
    assert second.parental_event_date == later_birth


# ---------------------------------------------------------------- the lapse


def test_the_quantum_refuses_after_the_suspension_ends(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    """It must not fall back to the pre-judgment s25A ten days — that would cut a
    parent from four months to ten days on a date nobody was watching."""
    with pytest.raises(ApplicationRefusedError) as raised:
        apply_for(
            employee,
            parental_type,
            owner,
            start=AFTER_LAPSE,
            end=AFTER_LAPSE,
            declared=declaration(event_date=AFTER_LAPSE),
        )
    message = str(raised.value)
    assert "Van Wyk" in message, message
    assert "03 October 2028" in message or "3 October 2028" in message, message


def test_an_adoption_outside_the_age_limit_is_refused_during_the_interim(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    adoption_type,
    parental_quantum,
    adoption_limits,
    owner,
):
    with pytest.raises(ApplicationRefusedError) as raised:
        apply_for(
            employee,
            adoption_type,
            owner,
            start=BIRTH,
            end=BIRTH,
            declared=declaration(child_under_age_limit=False),
        )
    message = str(raised.value)
    assert "2" in message and "s25B(1)" in message, message


def test_an_adoption_of_a_three_year_old_is_allowed_once_the_limit_falls_away(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    adoption_type,
    adoption_limits,
    owner,
    db,
):
    """5b, and it is DATA not code: the Court has already held the under-two
    limit invalid, so refusing this in November 2028 would be this software
    enforcing a struck-down provision. The quantum row for the later period is
    loaded here because the lapse of the QUANTUM is a different question."""
    from statutory.models import ParentalLeaveQuantum

    ParentalLeaveQuantum.objects.create(
        effective_from=datetime.date(2028, 10, 3),
        sole_parent_months=4,
        sole_parent_days=0,
        both_employed_months=4,
        both_employed_days=10,
        source_reference="Hypothetical remedial legislation, for this test only",
    )

    application = apply_for(
        employee,
        adoption_type,
        owner,
        start=AFTER_LAPSE,
        end=AFTER_LAPSE,
        declared=declaration(event_date=AFTER_LAPSE, child_under_age_limit=False),
    )
    assert application.status == LeaveApplication.Status.SUBMITTED


# --------------------------------------------------------------- one balance


def test_maternity_draws_on_the_parental_balance_not_its_own(maternity_type, parental_type):
    """2c: an employee must not take four months of maternity AND four months of
    parental leave for the same child."""
    from leave.cycles import resolve_balance_leave_type

    assert maternity_type.balance_source == LeaveType.BalanceSource.PARENT
    assert maternity_type.parent_leave_type_id == parental_type.pk
    assert resolve_balance_leave_type(maternity_type).pk == parental_type.pk


def test_maternity_and_parental_are_one_sequence_for_the_same_event(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    maternity_type,
    parental_type,
    parental_quantum,
    owner,
):
    apply_for(employee, maternity_type, owner, start=BIRTH, end=BIRTH + datetime.timedelta(days=20))
    with pytest.raises(ApplicationRefusedError) as raised:
        apply_for(
            employee,
            parental_type,
            owner,
            start=BIRTH + datetime.timedelta(days=60),
            end=BIRTH + datetime.timedelta(days=70),
        )
    assert "s25(4B)" in str(raised.value)


# ------------------------------------------------------------ unpaid, and pay


def test_parental_leave_is_unpaid_and_recorded_in_the_applications_own_unit(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
):
    """7a: through the same unpaid_days column as every other unpaid reason
    (D-188), not a third mechanism. And it draws on no balance."""
    application = apply_for(
        employee, parental_type, owner, start=BIRTH, end=BIRTH + datetime.timedelta(days=4)
    )
    assert application.unpaid_days == Decimal("5.000"), "Mon-Fri, all unpaid"
    assert application.unpaid_hours == Decimal("0")
    with tenant_context(employee.tenant_id):
        from leave.models import LeaveTransaction

        assert not LeaveTransaction.objects.filter(leave_application=application).exists()


def test_an_employer_may_elect_to_pay_parental_leave(
    employee,
    engagement,
    minimum_age,
    leave_rules,
    working_time_rules,
    schedule_5day,
    parental_type,
    parental_quantum,
    owner,
    employer,
):
    """7b: the statutory position is the default; a contract may be better."""
    from employers.models import EmployerSetting

    with tenant_context(employee.tenant_id):
        EmployerSetting.objects.create(
            tenant=employee.tenant,
            employer=employer,
            setting_key="PARENTAL_LEAVE_PAID",
            value_type=EmployerSetting.ValueType.BOOLEAN,
            value_boolean=True,
            set_by_employer=True,
        )

    application = apply_for(
        employee, parental_type, owner, start=BIRTH, end=BIRTH + datetime.timedelta(days=4)
    )
    assert application.unpaid_days == Decimal("0")
    with tenant_context(employee.tenant_id):
        paid = {d.is_paid for d in application.days.filter(is_working_day=True)}
    assert paid == {True}
