"""Where leave pay meets the database (D-220, D-221).

The calculator takes the s35(5) remuneration total already filtered. WHICH
components make up that total is `payroll_component.affects_leave_pay_average`,
decided once per component when the catalogue was seeded — so the flags are what
has to be held to the Minister's determination, and that is what this file does.

Government Notice 691 in Government Gazette 24889 of 23 May 2003, effective
1 July 2003, made under BCEA s35(5), applies "for the purposes of calculating
pay for annual leave in terms of section 21, payment instead of notice in terms
of section 38, and severance pay in terms of section 41". It INCLUDES, among
others, "(c) any cash payments made to an employee, except those listed as
exclusions", and EXCLUDES "(a) any cash payment or payment in kind provided to
enable the employee to work ... (c) gratuities ... and gifts from the employer
... (e) discretionary payments not related to an employee's hours of work or
performance".
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from calculators.base import StatutoryFigure
from calculators.leave_pay import AveragingWindow, LeavePayInput, leave_pay
from core.managers import tenant_context
from employers.models import PayrollComponent
from payroll.models import PayrollCalculationTrace
from payroll.tests.test_calculation_trace import employee  # noqa: F401 — fixture
from payroll.trace import record
from statutory import resolve
from statutory.models import StatutoryParameter

MARCH = datetime.date(2026, 3, 31)

#: BCEA s35(4)(a), loaded by ``reference/ref-2026.03.01-leave-pay.json``.
WINDOW_PARAMETER = "VARIABLE_EARNINGS_AVERAGE_WEEKS"

#: Every component whose pay counts toward the s35(4) average, and the limb of
#: GN 691 that puts it there. All six are cash payments for work actually done,
#: so GN 691(c) includes them and none of the exclusions reaches them.
COUNTS_TOWARD_THE_AVERAGE = {
    "BASIC": "GN 691 included (c) — a cash payment, and the wage itself",
    "OT_1_5": "GN 691 included (c); s35(5)(b)(iii) does not exclude it, being related to hours",
    "SUNDAY_2_0": "GN 691 included (c) — a cash payment related to hours of work",
    "PH_WORKED": "GN 691 included (c) — a cash payment related to hours of work",
    "NIGHT_ALLOW": "GN 691 included (c) — an allowance for WORKING nights, not to enable work",
    "STANDBY": "GN 691 included (c) — cash for being available, not for enabling work",
}


#: The catalogue cannot be seeded without the SARS source codes it points at, so
#: this reuses the fixture that already builds them rather than a second copy
#: that could drift from the flags the real fixture loads.
from employers.tests.test_payroll_components import source_codes  # noqa: E402, F401


@pytest.fixture
def seeded(source_codes):  # noqa: F811
    from employers.components import seed_system_components

    return seed_system_components()


# --------------------------------------------- the flags, against GN 691


@pytest.mark.django_db
def test_exactly_the_expected_components_count_toward_the_leave_pay_average(seeded):
    """The flag decides what an employee's leave is worth for the rest of their
    employment, and it is invisible on every screen. A component added later
    with the flag defaulted either way changes every fluctuating employee's
    leave pay and nothing announces it, so the set is pinned here by name."""
    flagged = set(
        PayrollComponent.objects.shared()
        .filter(affects_leave_pay_average=True)
        .values_list("code", flat=True)
    )

    assert flagged == set(COUNTS_TOWARD_THE_AVERAGE), (
        "The components feeding BCEA s35(4)'s average changed. Each one must be "
        "justified against GN 691's own lists before the set moves: "
        f"added {sorted(flagged - set(COUNTS_TOWARD_THE_AVERAGE))}, "
        f"removed {sorted(set(COUNTS_TOWARD_THE_AVERAGE) - flagged)}."
    )


@pytest.mark.django_db
def test_leave_pay_itself_never_feeds_its_own_average(seeded):
    """The catalogue's own reason, restated as a test: "affects_leave_pay_average
    is FALSE and must stay false: this is the output of the leave pay
    calculation, and feeding it back in would make a second period of leave
    compound off the first"."""
    leave_component = PayrollComponent.objects.shared().get(code="LEAVE_PAY")

    assert leave_component.affects_leave_pay_average is False


@pytest.mark.django_db
def test_the_statutory_deductions_are_not_remuneration(seeded):
    """GN 691 lists what remuneration INCLUDES. A deduction is not a payment to
    the employee at all, so no deduction or employer contribution may carry the
    flag — and one that did would inflate every average by the employer's own
    UIF and SDL."""
    wrongly_flagged = (
        PayrollComponent.objects.shared()
        .filter(affects_leave_pay_average=True)
        .exclude(component_type=PayrollComponent.ComponentType.EARNING)
    )

    assert not list(wrongly_flagged), [c.code for c in wrongly_flagged]


# ------------------------------------------- the window is loaded reference data


@pytest.mark.django_db
def test_the_averaging_window_resolves_from_reference_data_and_is_thirteen_weeks():
    StatutoryParameter.objects.create(
        parameter_code=WINDOW_PARAMETER,
        value_numeric=Decimal("13.000000"),
        unit="weeks",
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s35(4)(a)",
    )

    row = resolve.parameter(WINDOW_PARAMETER, MARCH)

    assert row.value_numeric == Decimal("13.000000")
    assert "s35(4)" in row.source_reference


@pytest.mark.django_db
def test_a_loaded_window_assembles_into_a_calculator_input():
    row = StatutoryParameter.objects.create(
        parameter_code=WINDOW_PARAMETER,
        value_numeric=Decimal("13.000000"),
        unit="weeks",
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s35(4)(a)",
    )

    result = leave_pay(_an_input(row.pk, row.value_numeric))

    assert result.trace.statutory_rows == (("statutory_parameter", row.pk),)
    assert result.amount.exact == Decimal("3000.000000")


def _an_input(row_id: int, weeks: Decimal) -> LeavePayInput:
    return LeavePayInput(
        calculated_for=MARCH,
        leave_days=Decimal("5.000"),
        leave_hours=Decimal("0"),
        daily_rate=Decimal("600.00"),
        hourly_rate=Decimal("66.666667"),
        remuneration_is_variable=True,
        window=AveragingWindow(
            weeks=StatutoryFigure(
                value=weeks, table=StatutoryParameter._meta.db_table, row_id=row_id
            ),
            weeks_available=Decimal("13"),
            remuneration=Decimal("39000.00"),
        ),
        days_per_week=Decimal("5"),
        hours_per_week=Decimal("45"),
    )


# -------------------------------------------------------------- and it stores


@pytest.mark.django_db
def test_a_leave_pay_trace_is_written_through_the_same_boundary_as_the_others(employee):  # noqa: F811
    result = leave_pay(_an_input(901, Decimal("13.000000")))
    row = record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)
    assert stored.calculator == "leave_pay.leave_pay"
    assert stored.statutory_rows == [["statutory_parameter", 901]]
    assert stored.outputs["average_weekly"] == "3000.000000"
    assert stored.outputs["amount"] == "3000.000000"
