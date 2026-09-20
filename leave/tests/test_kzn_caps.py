"""The prenatal and shop steward caps, enforced (D-269).

D-268 loaded the figures and gave them resolvers; **nothing read those
resolvers**, so an employer could pay eleven prenatal clinic days and the
system would record all eleven as paid. These are the caps.

**Neither is a balance and neither refuses.** Clause 13.2 grants three paid
clinic days and says nothing about a fourth, so a fourth is ordinary unpaid
time off the employer may still allow — what the cap decides is PAY, never
whether the leave may be taken. That is D-174's rule for an overdrawn
application, restated for a ceiling that comes from the instrument rather than
from a ledger, and parental leave (D-201) already works exactly this way.

The two facts these caps need are DECLARED, because nothing in this system
could compute either: the expected date of confinement, and whether an employee
holds a union office.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta

from core.managers import platform_context, tenant_context
from employees.engagements import engage
from employees.models import Employee, EmployeeUnionRole
from employers.models import Employer
from leave import applications
from leave.models import LeaveApplication, LeaveType
from leave.types import seed_system_leave_types
from statutory.loader import load_reference_data
from statutory.models import Sector, SectorArea

from .conftest import BORN, make_id

pytestmark = [pytest.mark.django_db]

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"
FIXTURES = (
    "ref-2026.04.01-bccci-maternity.json",
    "ref-2026.04.01-bccci-leave-types.json",
)


@pytest.fixture
def schedule_5day_for(tenant):
    """A Monday-to-Friday schedule for ANY employee — the conftest fixture is
    bound to the conftest employee, and these tests build their own."""
    from employees.models import WorkSchedule, WorkScheduleDay

    def build(employee, effective_from=datetime.date(2020, 1, 6)):
        with tenant_context(tenant.pk):
            made = WorkSchedule.objects.create(
                tenant=tenant,
                employee=employee,
                days_per_week=Decimal("5"),
                ordinary_hours_per_week=Decimal("40"),
                effective_from=effective_from,
            )
            for cycle_day in range(7):
                WorkScheduleDay.objects.create(
                    tenant=tenant,
                    work_schedule=made,
                    cycle_day=cycle_day,
                    is_working_day=cycle_day < 5,
                    ordinary_hours=Decimal("8") if cycle_day < 5 else Decimal("0"),
                )
        return made

    return build


#: A Wednesday, so every date below is an ordinary working day.
DUE = datetime.date(2026, 12, 2)
START = datetime.date(2026, 6, 1)


@pytest.fixture
def kzn(db, tenant, minimum_age, leave_rules, working_time_rules):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    area_b = SectorArea.objects.create(
        sector=sector, code="AREA_B", name="Area B", uses_bargaining_council_rates=True
    )
    for name in FIXTURES:
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))

    with tenant_context(tenant.pk):
        employer = Employer.objects.create(
            tenant=tenant, trading_name="Spotless KZN", sector=sector, sector_area=area_b
        )
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Nandi",
            last_name="Zulu",
            date_of_birth=BORN,
            mobile_number="+27820000021",
            email="nandi@example.com",
            id_number=make_id("5021"),
        )
    engage(employee, start_date=datetime.date(2020, 1, 6), job_title="Cleaner")
    return employee


def a_type(code):
    seed_system_leave_types()
    with platform_context():
        return LeaveType.objects.get(code=code, tenant__isnull=True)


def paid_days(application) -> int:
    """Pins the tenant. Without it RLS returns no rows and this reads 0 for
    every application — which would make every "is unpaid" assertion below pass
    for entirely the wrong reason. The count-reads-as-none trap, exactly as
    CLAUDE.md's table describes it."""
    with tenant_context(application.tenant_id):
        return application.days.filter(is_paid=True, is_working_day=True).count()


def working_days(application) -> int:
    with tenant_context(application.tenant_id):
        return application.days.filter(is_working_day=True).count()


def apply_prenatal(employee, on_date, *, due=DUE):
    return applications.submit_application(
        employee,
        leave_type=a_type("PRENATAL"),
        start_date=on_date,
        end_date=on_date,
        expected_date_of_confinement=due,
    )


def apply_steward(employee, start, end=None):
    return applications.submit_application(
        employee,
        leave_type=a_type("SHOP_STEWARD"),
        start_date=start,
        end_date=end or start,
    )


# ------------------------------------------------------------------- prenatal


def test_a_prenatal_day_with_no_due_date_is_refused(kzn, schedule_5day_for):
    """The one thing that IS refused, because without the date there is no
    window to be inside and the cap could not bite at all. A CHECK cannot say
    this — it cannot read through the leave_type foreign key — so the service
    does, and this watches it."""
    schedule_5day_for(kzn)

    with pytest.raises(applications.PrenatalDateRequiredError) as caught:
        applications.submit_application(
            kzn,
            leave_type=a_type("PRENATAL"),
            start_date=datetime.date(2026, 10, 7),
            end_date=datetime.date(2026, 10, 7),
        )

    assert "expected date of confinement" in str(caught.value)
    with tenant_context(kzn.tenant_id):
        assert LeaveApplication.objects.count() == 0, "refused means nothing written"


def test_one_paid_clinic_day_in_each_of_the_three_months(kzn, schedule_5day_for):
    """Clause 13.2, the ordinary case. Three separate applications, one in each
    one-month window counting back from 2 December: all three paid."""
    schedule_5day_for(kzn)

    for on_date in (
        datetime.date(2026, 9, 9),
        datetime.date(2026, 10, 7),
        datetime.date(2026, 11, 4),
    ):
        assert paid_days(apply_prenatal(kzn, on_date)) == 1, on_date


def test_a_second_day_in_the_same_month_is_unpaid(kzn, schedule_5day_for):
    """THE CAP FIRING. Two clinic visits in one window is one paid day and one
    unpaid — never a refusal, because the employer may still let her go."""
    schedule_5day_for(kzn)

    first = apply_prenatal(kzn, datetime.date(2026, 10, 7))
    second = apply_prenatal(kzn, datetime.date(2026, 10, 14))

    assert paid_days(first) == 1
    assert paid_days(second) == 0
    assert working_days(second) == 1, "the day still exists"
    assert second.status == LeaveApplication.Status.SUBMITTED, "submitted, not refused"


def test_a_fourth_day_outside_the_three_months_is_unpaid(kzn, schedule_5day_for):
    """Before the window opens there is no entitlement: the clause gives the
    days in the three months BEFORE the due date, not at any time."""
    schedule_5day_for(kzn)

    too_early = apply_prenatal(kzn, datetime.date(2026, 8, 5))

    assert paid_days(too_early) == 0


def test_a_revised_due_date_cannot_buy_a_second_paid_day_in_the_same_month(kzn, schedule_5day_for):
    """THIS TEST CHANGED THE DESIGN (D-269). The windows were one-month periods
    counted back from the due date, and they MOVE: shifting the declared date
    by ten days shifted every boundary and paid this day a second time. A
    revised due date is an ordinary event, so the windows became calendar
    months, which do not move."""
    schedule_5day_for(kzn)

    apply_prenatal(kzn, datetime.date(2026, 10, 7), due=DUE)
    again = apply_prenatal(kzn, datetime.date(2026, 10, 21), due=DUE + relativedelta(days=10))

    assert paid_days(again) == 0


def test_a_cancelled_clinic_day_gives_the_entitlement_back(kzn, schedule_5day_for):
    """A cancelled day was not taken. Counting it would charge an employee for
    an appointment she did not keep — the same reading authorisation.py takes
    when it reverses a cancelled application's ledger row."""
    schedule_5day_for(kzn)

    first = apply_prenatal(kzn, datetime.date(2026, 10, 7))
    first.status = LeaveApplication.Status.CANCELLED
    with tenant_context(kzn.tenant_id):
        first.save(update_fields=["status"])

    assert paid_days(apply_prenatal(kzn, datetime.date(2026, 10, 14))) == 1


def test_an_employer_the_agreement_does_not_bind_gets_no_paid_clinic_day(
    db, tenant, minimum_age, leave_rules, working_time_rules, schedule_5day_for
):
    """Watched NOT firing in the other direction. The BCEA has no prenatal
    clinic day, so a domestic worker's day is unpaid rather than paid by
    default — the resolver answers "not granted" and the cap honours it."""
    sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)
        employee = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=BORN,
            mobile_number="+27820000022",
            email="thandi2@example.com",
            id_number=make_id("5022"),
        )
    engage(employee, start_date=datetime.date(2020, 1, 6), job_title="Domestic worker")
    schedule_5day_for(employee)

    assert paid_days(apply_prenatal(employee, datetime.date(2026, 10, 7))) == 0


# -------------------------------------------------------------- shop steward


def a_role(employee, role, *, effective_from=datetime.date(2020, 1, 6)):
    with tenant_context(employee.tenant_id):
        return EmployeeUnionRole.objects.create(
            tenant_id=employee.tenant_id,
            employee=employee,
            role=role,
            trade_union_name="SATAWU",
            effective_from=effective_from,
        )


def test_an_employee_nobody_recorded_as_a_shop_steward_gets_nothing(kzn, schedule_5day_for):
    """NO ROW MEANS NO ENTITLEMENT, and that is the safe direction. Capping an
    unrecorded employee at six days would pay union leave to everybody."""
    schedule_5day_for(kzn)

    assert paid_days(apply_steward(kzn, datetime.date(2026, 6, 1))) == 0


def test_an_ordinary_shop_steward_gets_six_paid_days_a_year(kzn, schedule_5day_for):
    """Clause 20.4(a)(ii)."""
    schedule_5day_for(kzn)
    a_role(kzn, EmployeeUnionRole.Role.SHOP_STEWARD)

    taken = apply_steward(kzn, datetime.date(2026, 6, 1), datetime.date(2026, 6, 8))

    assert paid_days(taken) == 6, "six working days paid out of the six-day span"


def test_an_office_bearer_gets_four_and_the_seventh_day_is_unpaid(kzn, schedule_5day_for):
    """THE CAP FIRING, and on the SMALLER figure. Clause 20.4(a)(i) gives an
    office bearer four days where an ordinary steward gets six — which reads
    like the gazette swapped its limbs, and is answered as printed (O-32)."""
    schedule_5day_for(kzn)
    a_role(kzn, EmployeeUnionRole.Role.OFFICE_BEARER)

    taken = apply_steward(kzn, datetime.date(2026, 6, 1), datetime.date(2026, 6, 8))

    assert paid_days(taken) == 4
    assert working_days(taken) == 6, "all six days still exist"


def test_the_cap_counts_days_already_taken_earlier_in_the_year(kzn, schedule_5day_for):
    """Four days in March leaves nothing for June — the cap is annual, not
    per application."""
    schedule_5day_for(kzn)
    a_role(kzn, EmployeeUnionRole.Role.OFFICE_BEARER)

    apply_steward(kzn, datetime.date(2026, 5, 4), datetime.date(2026, 5, 7))
    later = apply_steward(kzn, datetime.date(2026, 6, 1))

    assert paid_days(later) == 0


def test_the_cap_resets_in_the_next_calendar_year(kzn, schedule_5day_for):
    """D-269 reads "per year" as the CALENDAR year, because clause 4.5(d) is
    the only place this agreement defines a year and it says "Calendar Year".
    Flagged for the labour law review (O-06): an employment-anniversary year is
    the other defensible reading and would move which days are paid."""
    schedule_5day_for(kzn)
    a_role(kzn, EmployeeUnionRole.Role.OFFICE_BEARER)

    apply_steward(kzn, datetime.date(2026, 5, 4), datetime.date(2026, 5, 7))

    assert paid_days(apply_steward(kzn, datetime.date(2027, 3, 1))) == 1


def test_a_role_that_has_ended_no_longer_earns_the_leave(kzn, schedule_5day_for):
    """Effective-dated, so a steward who stood down in April is not one in
    June. Invariant 2 is why this is a row and not a boolean."""
    schedule_5day_for(kzn)
    role = a_role(kzn, EmployeeUnionRole.Role.OFFICE_BEARER)
    with tenant_context(kzn.tenant_id):
        role.effective_to = datetime.date(2026, 4, 1)
        role.save(update_fields=["effective_to"])

    assert paid_days(apply_steward(kzn, datetime.date(2026, 6, 1))) == 0


def test_neither_type_ever_touches_a_leave_balance(kzn, schedule_5day_for):
    """Neither keeps one (D-268), so an application against either must not
    report an overdraw against a zero balance — which is what the old code
    would have done the moment these types reached submit_application()."""
    schedule_5day_for(kzn)
    a_role(kzn, EmployeeUnionRole.Role.SHOP_STEWARD)

    for application in (
        apply_prenatal(kzn, datetime.date(2026, 10, 7)),
        apply_steward(kzn, datetime.date(2026, 6, 1)),
    ):
        assert application.exceeds_balance is False
        with tenant_context(application.tenant_id):
            assert not application.days.filter(deducted_from_balance=True).exists()
