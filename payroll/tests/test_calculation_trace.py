"""The trace: invariant 5, and the boundary that keeps calculators pure (D-208).

The calculator PRODUCES the structure and the caller PERSISTS it. So these tests
run a real calculator — no fixture trace hand-built to match — and hand what it
returned to ``payroll.trace.record()``.

No payslip and no run: P7's assembly is blocked on P2 verification, and this
chunk deliberately does not build it. ``payslip_id_ref`` is a forward reference
until it can be a real foreign key.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from calculators.base import StatutoryFigure
from calculators.sdl import SdlInput, levy
from calculators.uif import UifInput, contribution
from core.managers import tenant_context
from core.models import Tenant
from employees.engagements import MINIMUM_AGE_PARAMETER, engage
from employees.identity import luhn_check_digit
from employees.models import Employee
from employers.models import Employer
from payroll.models import PayrollCalculationTrace
from payroll.trace import record
from statutory.models import Sector, StatutoryParameter

pytestmark = pytest.mark.django_db

MARCH = datetime.date(2026, 3, 31)

CEILING = StatutoryFigure(
    value=Decimal("17712.00"), table="statutory_parameter", row_id=901, description="ceiling"
)
RATE = StatutoryFigure(
    value=Decimal("1.000000"), table="statutory_parameter", row_id=902, description="employee rate"
)
EMPLOYER_RATE = StatutoryFigure(
    value=Decimal("1.000000"), table="statutory_parameter", row_id=903, description="employer rate"
)


@pytest.fixture
def employee(db):
    StatutoryParameter.objects.create(
        parameter_code=MINIMUM_AGE_PARAMETER,
        value_numeric=Decimal("15.000000"),
        unit=StatutoryParameter.Unit.YEARS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="BCEA 75 of 1997, s43(1)",
    )
    sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    tenant = Tenant.objects.create(trading_name="Household")
    body = "900101500908"
    with tenant_context(tenant.pk):
        employer = Employer.objects.create(tenant=tenant, trading_name="Household", sector=sector)
        person = Employee.objects.create(
            tenant=tenant,
            employer=employer,
            first_name="Thandi",
            last_name="Mokoena",
            date_of_birth=datetime.date(1990, 1, 1),
            mobile_number="+27820000001",
            email="thandi@example.com",
            id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
        )
    engage(person, start_date=datetime.date(2026, 3, 1), job_title="Domestic worker")
    return person


def a_uif_result(**overrides):
    values = {
        "calculated_for": MARCH,
        "remuneration": Decimal("10000.00"),
        "commission": Decimal("0.00"),
        "excluded_remuneration": Decimal("0.00"),
        "monthly_ceiling": CEILING,
        "employee_rate_percent": RATE,
        "employer_rate_percent": EMPLOYER_RATE,
        "is_exempt": False,
        "exemption_reason": "",
    }
    values.update(overrides)
    return contribution(UifInput(**values))


def test_what_the_calculator_returned_is_what_is_stored(employee):
    result = a_uif_result()

    row = record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)
    assert stored.calculator_name == "uif.contribution"
    assert stored.calculated_for == MARCH
    assert stored.inputs["remuneration"] == "10000.00"
    assert stored.outputs["employee"] == "100.000000", "unrounded, per invariant 6"
    assert stored.reference_rows_used == [
        ["statutory_parameter", 901],
        ["statutory_parameter", 902],
        ["statutory_parameter", 903],
    ]


def test_the_keys_stored_can_be_resolved_back_to_rows(employee):
    """Keys, not citation text: a citation can be corrected later (two have been
    in this build), and the key still opens the row the figure came from."""
    row = record(employee, a_uif_result().trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(pk=row.pk)
    tables = {table for table, _ in stored.reference_rows_used}
    assert tables == {"statutory_parameter"}
    assert all(isinstance(key, int) for _, key in stored.reference_rows_used)


def test_a_zero_is_traced_exactly_like_any_other_figure(employee):
    """A missing trace row means "this never ran" and must not be able to mean
    anything else — so an exempt employee, contributing nothing, still writes."""
    result = a_uif_result(
        is_exempt=True, exemption_reason="Less than 24 hours a month (UICA s4(1)(a))"
    )

    record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(employee=employee)
    assert stored.outputs["employee"] == "0.000000"
    assert "s4(1)(a)" in stored.outputs["exemption_reason"]


def test_a_warning_is_stored_with_the_calculation_that_produced_it(employee):
    result = a_uif_result(remuneration=Decimal("1000.00"), commission=Decimal("1500.00"))

    record(employee, result.trace)

    with tenant_context(employee.tenant_id):
        stored = PayrollCalculationTrace.objects.get(employee=employee)
    assert stored.warnings and "exceed" in stored.warnings[0]


def test_two_calculators_for_one_employee_leave_two_traces(employee):
    """Per payslip PER CALCULATOR: SDL and UIF each answer for themselves."""
    record(employee, a_uif_result().trace)
    sdl = levy(
        SdlInput(
            calculated_for=MARCH,
            leviable_amount=Decimal("10000.00"),
            rate_percent=StatutoryFigure(
                value=Decimal("1.000000"), table="statutory_parameter", row_id=801
            ),
            employer_is_liable=True,
        )
    )
    record(employee, sdl.trace)

    with tenant_context(employee.tenant_id):
        names = set(
            PayrollCalculationTrace.objects.filter(employee=employee).values_list(
                "calculator_name", flat=True
            )
        )
    assert names == {"uif.contribution", "sdl.levy"}


# ------------------------------------------------------------- never rewritten


def test_a_trace_cannot_be_updated(employee):
    """Same family as a ledger row. A trace that can be edited after the fact is
    not evidence of anything, so a trigger holds it rather than a convention."""
    row = record(employee, a_uif_result().trace)

    with pytest.raises(DatabaseError) as raised, transaction.atomic():
        with tenant_context(employee.tenant_id):
            PayrollCalculationTrace.objects.filter(pk=row.pk).update(calculator_name="uif.tampered")

    assert "append-only" in str(raised.value).lower(), str(raised.value)


def test_a_trace_cannot_be_deleted(employee):
    row = record(employee, a_uif_result().trace)

    with pytest.raises(DatabaseError), transaction.atomic():
        with tenant_context(employee.tenant_id):
            PayrollCalculationTrace.objects.filter(pk=row.pk).delete()

    with tenant_context(employee.tenant_id):
        assert PayrollCalculationTrace.objects.filter(pk=row.pk).exists()


def test_a_trace_must_name_its_calculator(employee):
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        with tenant_context(employee.tenant_id):
            PayrollCalculationTrace.objects.create(
                tenant=employee.tenant,
                employee=employee,
                calculator_name="",
                calculated_for=MARCH,
                inputs={},
                outputs={},
            )
    assert "calculation_trace_names_its_calculator" in str(raised.value)


def test_row_level_security_is_forced_on_the_trace_table():
    """The generated isolation suite covers this too; asserted here as well
    because leave/0003 shipped three tables without enable_rls() and the suite
    was the only thing that noticed."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'payroll_calculation_trace'::regclass"
        )
        enabled, forced = cursor.fetchone()
    assert enabled and forced
