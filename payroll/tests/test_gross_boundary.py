"""Where the pure gross pay calculator meets the database (D-216).

``calculators/gross.py`` carries two hand-copied enums, because a calculator may
not import a model: ``PayBasis`` copies ``EmployeeRemuneration.PayBasis`` and
``NightAllowanceKind`` copies ``statutory.NightAllowanceType``. A copy nobody
compares is how a sixth pay basis, or a fifth night allowance type, quietly
stops being recognised — the basis would fall through to the salaried branch and
pay a salary to an hourly employee.

The rest is wiring: the row ``statutory.resolve`` hands back must assemble into
``PremiumRates`` with nothing missing, and the trace must persist through the
same boundary UIF, SDL and PAYE use (D-208).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.attendance import AttendanceDayInput, AttendanceDayResult, DayType
from calculators.gross import (
    DayPay,
    GrossInput,
    NightAllowanceKind,
    PayBasis,
    PremiumRates,
    gross_pay,
)
from core.managers import tenant_context
from employees.models import EmployeeRemuneration
from payroll.models import PayrollCalculationTrace
from payroll.tests.test_calculation_trace import employee  # noqa: F401 — fixture
from payroll.trace import record
from statutory import resolve
from statutory.models import NightAllowanceType, Sector, WorkingTimeRuleSet

MARCH = datetime.date(2026, 3, 31)


# ------------------------------------------------------------- the two enums


def test_the_calculator_knows_exactly_the_pay_bases_the_remuneration_row_can_hold():
    assert {basis.value for basis in PayBasis} == set(EmployeeRemuneration.PayBasis.values)


def test_the_calculator_knows_exactly_the_night_allowance_types_the_rule_set_can_hold():
    assert {kind.value for kind in NightAllowanceKind} == set(NightAllowanceType.values)


def test_the_pay_bases_match_the_rate_derivation_module_too():
    """``employees/rates.py`` converts every basis to a weekly rate and raises on
    anything it does not know. If it and the calculator disagree, one of them
    refuses a basis the other prices."""
    from employees.rates import RateDerivationError, WorkingPattern, weekly_rate_from

    pattern = WorkingPattern(
        hours_per_day=Decimal("9"), days_per_week=Decimal("5"), hours_per_week=Decimal("45")
    )
    for basis in PayBasis:
        assert weekly_rate_from(basis.value, Decimal("1000"), pattern, Decimal("4.333")) > Decimal(
            "0"
        )

    with pytest.raises(RateDerivationError):
        weekly_rate_from("quarterly", Decimal("1000"), pattern, Decimal("4.333"))


# ----------------------------------------- the reference data fits the input


@pytest.mark.django_db
def test_a_loaded_rule_set_assembles_into_a_calculator_input():
    sector = Sector.objects.create(code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning")
    WorkingTimeRuleSet.objects.create(
        sector=sector,
        effective_from=datetime.date(2026, 3, 1),
        source_reference="Sectoral Determination 1: Contract Cleaning Sector, clauses 8 to 17",
        ordinary_hours_per_week=Decimal("45"),
        ordinary_hours_per_day_5day=Decimal("9"),
        ordinary_hours_per_day_6day=Decimal("8"),
        overtime_multiplier=Decimal("1.500"),
        max_overtime_hours_per_day=Decimal("3"),
        max_overtime_hours_per_week=Decimal("10"),
        sunday_multiplier_ordinary=Decimal("1.500"),
        sunday_multiplier_non_ordinary=Decimal("2.000"),
        public_holiday_worked_multiplier=Decimal("2.000"),
        public_holiday_not_worked_paid=True,
        night_work_start_time=datetime.time(18, 0),
        night_work_end_time=datetime.time(6, 0),
        night_allowance_type=NightAllowanceType.PERCENTAGE,
        night_allowance_value=Decimal("10.0000"),
        standby_allowance_per_shift=Decimal("0"),
        standby_window_start=datetime.time(0, 0),
        standby_window_end=datetime.time(0, 0),
        standby_hours_before_overtime=Decimal("0"),
        min_paid_hours_per_day=Decimal("6"),
        meal_interval_after_hours=Decimal("5"),
        meal_interval_minutes=60,
        daily_rest_hours=12,
        weekly_rest_hours=36,
        accommodation_deduction_capped=False,
        accommodation_deduction_max_pct=None,
    )

    row = resolve.working_time_rules(sector, MARCH)
    allowance = resolve.night_allowance(sector, MARCH)

    rates = PremiumRates(
        overtime_multiplier=row.overtime_multiplier,
        sunday_multiplier_ordinary=row.sunday_multiplier_ordinary,
        sunday_multiplier_non_ordinary=row.sunday_multiplier_non_ordinary,
        public_holiday_worked_multiplier=row.public_holiday_worked_multiplier,
        public_holiday_not_worked_paid=row.public_holiday_not_worked_paid,
        night_allowance_type=NightAllowanceKind(allowance.state.value),
        night_allowance_value=allowance.value,
        table=WorkingTimeRuleSet._meta.db_table,
        row_id=row.pk,
    )

    result = gross_pay(_an_input(rates))

    assert result.trace.statutory_rows == (("working_time_rule_set", row.pk),)
    # Nine ordinary hours at R45, plus 10% of R45 for each of four night hours.
    assert result.gross.exact == Decimal("423.000000")


def _an_input(rates: PremiumRates) -> GrossInput:
    return GrossInput(
        calculated_for=MARCH,
        pay_basis=PayBasis.HOURLY,
        days=(
            DayPay(
                day=AttendanceDayInput(
                    work_date=MARCH,
                    day_type=DayType.ORDINARY,
                    time_in=None,
                    time_out=None,
                    unpaid_break_minutes=0,
                    is_standby=False,
                    is_ordinary_working_day=True,
                    scheduled_ordinary_hours=Decimal("9.00"),
                ),
                hours=AttendanceDayResult(
                    ordinary_hours=Decimal("9.00"),
                    overtime_hours=Decimal("0"),
                    sunday_hours=Decimal("0"),
                    public_holiday_hours=Decimal("0"),
                    night_hours=Decimal("4.00"),
                    paid_hours_guaranteed=Decimal("0"),
                    standby_hours_worked=Decimal("0"),
                    days_worked_equivalent=Decimal("1.000"),
                ),
            ),
        ),
        rates=rates,
        hourly_rate=Decimal("45.00"),
        daily_rate=Decimal("405.00"),
        ordinary_shift_hours=Decimal("9.00"),
    )


# -------------------------------------------------------------- and it stores


@pytest.mark.django_db
def test_a_gross_pay_trace_is_written_through_the_same_boundary_as_the_others(employee):  # noqa: F811
    rates = PremiumRates(
        overtime_multiplier=Decimal("1.500"),
        sunday_multiplier_ordinary=Decimal("1.500"),
        sunday_multiplier_non_ordinary=Decimal("2.000"),
        public_holiday_worked_multiplier=Decimal("2.000"),
        public_holiday_not_worked_paid=True,
        night_allowance_type=NightAllowanceKind.BY_AGREEMENT,
        night_allowance_value=None,
        table="working_time_rule_set",
        row_id=601,
    )

    result = gross_pay(_an_input(rates))
    row = record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)
    assert stored.calculator == "gross.gross_pay"
    assert stored.statutory_rows == [["working_time_rule_set", 601]]
    assert stored.outputs["line_BASIC"] == "405.000000"
    assert stored.warnings and "states no allowance" in stored.warnings[0]
