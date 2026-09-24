"""The payroll component catalogue — and the first table with a third tenancy shape.

Two things are being proven here, and they are separable.

**The catalogue is right.** Sixteen components, each pointing at the SARS source
code that decides its IRP5 line, and none of them carrying a statutory rate of its
own. The tests that matter are the ones that fail when somebody helpfully types
``1.5`` into ``default_rate_multiplier`` on the overtime row, or points severance at
3601 to make it work.

**The shared tenancy shape is safe.** ``TenantSharedModel`` lets a NULL tenant row be
read by every tenant, which is a thing the other two bases exist to prevent. So the
guarantee has to be re-proved from scratch here rather than inherited: a tenant reads
the catalogue and its own additions, and nothing else — not another tenant's rows,
and not the ability to change or delete the shared ones.

The delete case is the sharp one. A DELETE is checked against a policy's USING clause
only, because there is no new row for a WITH CHECK to test. The policy therefore
cannot stop a tenant deleting a shared row it can legitimately see, and the trigger
is the only thing that can.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction

from core.managers import platform_context, tenant_context
from core.models import Tenant
from employers.components import (
    SYSTEM_COMPONENTS,
    ComponentSeedError,
    seed_system_components,
)
from employers.models import PayrollComponent
from statutory.models import SarsSourceCode

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------- fixtures


CODE_FLAGS = {
    "3601": (True, True, True, True),
    "3605": (True, True, True, True),
    "3607": (True, True, True, True),
    "3713": (True, True, True, True),
    # 3901 is taxable but outside the UIF and SDL bases: both statutes exclude
    # paragraph (d) gross income amounts, and SARS places severance benefits there.
    "3901": (True, False, False, False),
    "4102": (False, False, False, False),
    "4141": (False, False, False, False),
    "4142": (False, False, False, False),
}


@pytest.fixture
def source_codes(db):
    """The seven codes the catalogue points at, with the flags the fixture loads."""
    made = {}
    for code, (taxable, uif, sdl, coida) in CODE_FLAGS.items():
        made[code] = SarsSourceCode.objects.create(
            code=code,
            description=f"Test code {code}",
            code_group=(
                SarsSourceCode.Group.DEDUCTION
                if code.startswith("4")
                else SarsSourceCode.Group.INCOME
            ),
            is_taxable=taxable,
            is_uif_remuneration=uif,
            is_sdl_remuneration=sdl,
            is_coida_remuneration=coida,
            source_reference="Test fixture",
        )
    return made


@pytest.fixture
def seeded(source_codes):
    return seed_system_components()


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(trading_name="Employer A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(trading_name="Employer B")


def own_component(tenant, **overrides):
    values = {
        "code": "TRANSPORT",
        "name": "Transport allowance",
        "component_type": PayrollComponent.ComponentType.EARNING,
        "calculation_method": PayrollComponent.CalculationMethod.FIXED,
        **overrides,
    }
    with tenant_context(tenant.pk):
        return PayrollComponent.objects.create(tenant=tenant, **values)


# ------------------------------------------------------------------ the catalogue


def test_seeding_creates_every_component_the_workbook_names(seeded):
    expected = {spec.code for spec in SYSTEM_COMPONENTS}
    assert {c.code for c in seeded} == expected
    assert len(expected) == 17, "Sheet 02's sixteen, and LEAVE_PAYOUT (D-306)."


def test_the_codes_are_exactly_the_ones_in_sheet_02(seeded):
    """Typed out rather than derived, so a rename has to be a deliberate edit here.
    Sheet 02's sixteen plus one named addition."""
    assert {spec.code for spec in SYSTEM_COMPONENTS} == {
        "BASIC",
        "OT_1_5",
        "SUNDAY_2_0",
        "PH_WORKED",
        "NIGHT_ALLOW",
        "STANDBY",
        "LEAVE_PAY",
        # D-306: NOT in sheet 02. SARS G06 p7 puts leave paid out on termination
        # under 3605, and LEAVE_PAY is 3601.
        "LEAVE_PAYOUT",
        "BONUS_PRO_RATA",
        "SEVERANCE",
        "NOTICE_PAY",
        "UIF_EE",
        "UIF_ER",
        "SDL_ER",
        "PAYE",
        "ACCOM_DED",
        "ADVANCE_DED",
    }


def test_seeding_twice_creates_nothing_and_changes_nothing(seeded):
    """A finalised payslip line points at these rows."""
    with platform_context():
        before = {c.code: (c.pk, c.updated_at) for c in PayrollComponent.objects.shared()}

    assert seed_system_components() == []

    with platform_context():
        after = {c.code: (c.pk, c.updated_at) for c in PayrollComponent.objects.shared()}
    assert before == after


def test_seeding_refuses_when_the_source_codes_are_not_loaded(db):
    """A fresh clone has empty reference tables. Refuse rather than create orphans."""
    with pytest.raises(ComponentSeedError) as caught:
        seed_system_components()
    assert "loadstatutory" in str(caught.value)

    with platform_context():
        assert PayrollComponent.objects.shared().count() == 0, (
            "A refused seed must write nothing at all."
        )


# ------------------------------------------------------ no statutory rate lives here


def test_no_system_component_carries_a_rate_of_its_own(seeded):
    """THE ONE THAT KEEPS A MARCH GAZETTE WORKING.

    ``OT_1_5`` is a label. The 1.5 is BCEA s10 read through
    ``working_time_rule_set.overtime_multiplier``, which is effective-dated, differs
    by sector, and gets a new row when it changes. A multiplier stored here would be
    a hard-coded statutory rate wearing a data row as a disguise: the gazette would
    be loaded, the table updated, and every overtime line would still use the old
    figure with nothing reporting a problem.
    """
    offenders = [
        c.code
        for c in seeded
        if c.default_rate_multiplier is not None or c.percentage_value is not None
    ]
    assert not offenders, (
        f"System components carrying their own rate: {offenders}. Statutory figures "
        f"live in working_time_rule_set with a gazette citation and are read through "
        f"statutory.resolve. The two rate columns are for employer-defined components."
    )


def test_every_component_whose_figure_is_gazetted_is_marked_statutory(seeded):
    by_code = {c.code: c for c in seeded}
    gazetted = {
        "OT_1_5",
        "SUNDAY_2_0",
        "PH_WORKED",
        "NIGHT_ALLOW",
        "STANDBY",
        "PAYE",
        "UIF_EE",
        "UIF_ER",
        "SDL_ER",
    }
    for code in gazetted:
        assert by_code[code].calculation_method == PayrollComponent.CalculationMethod.STATUTORY, (
            f"{code}'s figure comes from reference data, so its method is 'statutory'."
        )


def test_an_employer_may_still_set_its_own_rate(tenant_a):
    """The two rate columns are not dead. They are for numbers nobody gazetted."""
    component = own_component(
        tenant_a,
        code="SHIFT_PREMIUM",
        default_rate_multiplier=Decimal("1.75"),
    )
    assert component.default_rate_multiplier == Decimal("1.7500")


# ------------------------------------------------ the tax treatment is not decided here


def test_every_component_with_a_source_code_agrees_with_it(seeded):
    for component in seeded:
        if component.sars_source_code_id is None:
            continue
        code = component.sars_source_code
        assert component.is_taxable == code.is_taxable
        assert component.is_uif_base == code.is_uif_remuneration
        assert component.is_sdl_base == code.is_sdl_remuneration
        assert component.is_coida_base == code.is_coida_remuneration


def test_a_component_contradicting_its_source_code_is_refused(source_codes):
    """Duplicating a compliance decision is how the two copies come to disagree.

    The way it surfaces otherwise is an EMP201 that does not reconcile to the
    payslips behind it, months later.
    """
    component = PayrollComponent(
        tenant=None,
        code="MADE_UP",
        name="Not taxable, apparently",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_method=PayrollComponent.CalculationMethod.FIXED,
        sars_source_code=source_codes["3601"],
        is_taxable=False,
        is_system=True,
    )
    with pytest.raises(ValidationError) as caught:
        component.full_clean(exclude=["tenant"])
    assert "3601" in str(caught.value)
    assert "single source of truth" in str(caught.value)


def test_commission_style_split_bases_survive_the_copy(source_codes, seeded):
    """The four flags are separate columns because the bases genuinely differ.

    Commission is excluded from the UIF contribution base while bonuses are not, so
    a single is_taxable would quietly get one of them wrong. No system component is
    commission, but the mechanism has to carry the distinction for the ones P4 adds.
    """
    commission = SarsSourceCode.objects.create(
        code="3606",
        description="Commission",
        code_group=SarsSourceCode.Group.INCOME,
        is_taxable=True,
        is_uif_remuneration=False,
        is_sdl_remuneration=True,
        is_coida_remuneration=True,
        source_reference="Test fixture",
    )
    component = PayrollComponent(
        tenant=None,
        code="COMMISSION",
        name="Commission",
        component_type=PayrollComponent.ComponentType.EARNING,
        calculation_method=PayrollComponent.CalculationMethod.FIXED,
        sars_source_code=commission,
        is_taxable=True,
        is_uif_base=False,
        is_sdl_base=True,
        is_coida_base=True,
        is_system=True,
    )
    component.full_clean(exclude=["tenant"])  # does not raise


# ------------------------------------------------------------------- leave pay


def test_the_leave_pay_flag_matches_the_determination(seeded):
    """BCEA s35(5), GN 691 in Government Gazette 24889, in force 1 July 2003.

    Its rule is a catch-all: any cash payment forms part of remuneration except
    those on a short exclusion list — payments enabling work, relocation,
    gratuities, share schemes, discretionary payments unrelated to hours or
    performance, entertainment, education. Overtime, Sunday work, public holiday
    work, night allowances and standby are none of those.

    Written out as an exact set rather than a rule, so that changing any one of
    them is an edit to this list with the citation sitting next to it.
    """
    included = {c.code for c in seeded if c.affects_leave_pay_average}
    assert included == {
        "BASIC",
        "OT_1_5",
        "SUNDAY_2_0",
        "PH_WORKED",
        "NIGHT_ALLOW",
        "STANDBY",
    }


def test_leave_pay_does_not_feed_its_own_average(seeded):
    """Otherwise a second period of leave compounds off the first."""
    by_code = {c.code: c for c in seeded}
    assert by_code["LEAVE_PAY"].affects_leave_pay_average is False


# --------------------------------------------------------- what is NOT settled


def test_severance_is_live_against_3901(seeded):
    """The gap D-90 recorded, closed by D-114 when SARS's own code guide arrived.

    It shipped inactive with no source code because pointing it at 3601 would have
    put a termination payment on the wrong IRP5 line and taxed it at the wrong rate.
    3901 is "Gratuities / Severance Benefits", and the guide's third qualifying limb
    — "services terminated due to reduction of personnel" — is dismissal for
    operational requirements, the only ground on which BCEA s41 severance is due.
    """
    by_code = {c.code: c for c in seeded}
    severance = by_code["SEVERANCE"]
    assert severance.is_active is True
    assert severance.sars_source_code.code == "3901"


def test_severance_is_outside_the_uif_and_sdl_bases(seeded):
    """Both statutes exclude paragraph (d) gross income, and SARS puts 3901 there.

    Taxable, because the guide says "(Subject to PAYE)" — on a directive against the
    retirement lump sum table, which is the termination engine's problem rather than
    this flag's.
    """
    severance = next(c for c in seeded if c.code == "SEVERANCE")
    assert severance.is_taxable is True
    assert severance.is_uif_base is False
    assert severance.is_sdl_base is False


def test_the_only_components_without_a_source_code_are_the_two_expected(seeded):
    """A component with no source code has no IRP5 line, so the list stays short."""
    assert {c.code for c in seeded if c.sars_source_code_id is None} == {
        "ACCOM_DED",
        "ADVANCE_DED",
    }


def test_a_component_with_no_source_code_taxes_nothing(seeded):
    for component in seeded:
        if component.sars_source_code_id is not None:
            continue
        assert not any(
            (
                component.is_taxable,
                component.is_uif_base,
                component.is_sdl_base,
                component.is_coida_base,
            )
        ), f"{component.code} has no source code, so it has no IRP5 line to sit on."


def test_every_component_records_why_it_is_treated_as_it_is():
    """A compliance decision with no recorded reason is one nobody dares change."""
    for spec in SYSTEM_COMPONENTS:
        assert len(spec.reason) > 80, f"{spec.code} needs a real reason, not a label."


# ------------------------------------------------------------------ shared tenancy


def test_a_tenant_reads_the_catalogue_and_its_own(tenant_a, seeded):
    own_component(tenant_a)
    with tenant_context(tenant_a.pk):
        codes = set(PayrollComponent.objects.values_list("code", flat=True))
    assert "BASIC" in codes, "A tenant must see the shared catalogue."
    assert "TRANSPORT" in codes


def test_a_tenant_never_sees_another_tenants_component(tenant_a, tenant_b, seeded):
    """The guarantee the shared base must not weaken."""
    own_component(tenant_b, code="B_ONLY")

    with tenant_context(tenant_a.pk):
        assert not PayrollComponent.objects.filter(code="B_ONLY").exists()
        assert not PayrollComponent.all_tenants.filter(code="B_ONLY").exists(), (
            "all_tenants bypasses the manager, not the policy. Row-level security "
            "is not doing its job."
        )


def test_own_and_shared_split_the_catalogue(tenant_a, seeded):
    own_component(tenant_a)
    with tenant_context(tenant_a.pk):
        assert set(PayrollComponent.objects.own().values_list("code", flat=True)) == {"TRANSPORT"}
        assert "TRANSPORT" not in set(
            PayrollComponent.objects.shared().values_list("code", flat=True)
        )


def test_a_tenant_cannot_create_a_shared_component(tenant_a, seeded):
    """WITH CHECK, not application code. The manager cannot stop a write."""
    with (
        tenant_context(tenant_a.pk),
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
    ):
        PayrollComponent.objects.create(
            tenant=None,
            code="SNEAKY",
            name="Shared by the back door",
            component_type=PayrollComponent.ComponentType.EARNING,
            is_system=True,
        )
    # Either layer refusing is correct: the policy's WITH CHECK clause, or the CHECK
    # constraint saying a shared row must be a system row. What must not happen is
    # the row landing in everybody's catalogue.
    assert isinstance(caught.value, DatabaseError)


def test_a_tenant_cannot_edit_a_system_component(tenant_a, seeded):
    """An UPDATE is tested against USING and WITH CHECK, and the trigger besides.

    Either layer refusing is the right outcome. What must not happen is the update
    landing, because every other employer on the platform reads this row.
    """
    with tenant_context(tenant_a.pk):
        basic = PayrollComponent.objects.get(code="BASIC")
        with pytest.raises(DatabaseError), transaction.atomic():
            PayrollComponent.objects.filter(pk=basic.pk).update(name="Renamed")

    with platform_context():
        assert PayrollComponent.objects.get(code="BASIC").name == "Basic wage"


def test_a_tenant_cannot_delete_a_system_component(tenant_a, seeded):
    """THE ONE THE POLICY CANNOT CATCH.

    A DELETE is checked against the policy's USING clause only — there is no WITH
    CHECK for a DELETE, because there is no new row to check. The tenant can
    legitimately see this row, so USING passes and the delete succeeds, and the
    catalogue every other employer's payslips point at loses a line.

    ``lock_system_rows()`` is the only layer left, for the same reason
    ``no_delete()`` exists on the reference tables: a trigger runs regardless of
    what the deleting session can see, and it binds the table owner.
    """
    with tenant_context(tenant_a.pk):
        with pytest.raises(DatabaseError) as caught, transaction.atomic():
            PayrollComponent.objects.filter(code="BASIC").delete()
    assert "system row" in str(caught.value).lower()

    with platform_context():
        assert PayrollComponent.objects.shared().filter(code="BASIC").exists()


def test_the_platform_cannot_casually_edit_one_either(seeded):
    """The lock binds the platform too, and deliberately.

    ``platform_context()`` opens the policy, not the trigger. Changing a system
    component's treatment is a migration with a reason attached, because finalised
    payslip lines point at it.
    """
    with platform_context(), pytest.raises(DatabaseError), transaction.atomic():
        PayrollComponent.objects.filter(code="PAYE").update(is_taxable=True)


def test_a_tenant_may_edit_and_delete_its_own(tenant_a, seeded):
    """The lock is about system rows, not about the table."""
    component = own_component(tenant_a)
    with tenant_context(tenant_a.pk):
        PayrollComponent.objects.filter(pk=component.pk).update(name="Renamed")
        assert PayrollComponent.objects.get(pk=component.pk).name == "Renamed"
        PayrollComponent.objects.filter(pk=component.pk).delete()
        assert not PayrollComponent.objects.filter(pk=component.pk).exists()


def test_raw_sql_from_a_tenant_session_sees_the_same_rows(tenant_a, tenant_b, seeded):
    """Layer 2 catches what bypasses the ORM entirely.

    Worth writing out because row-level security applies to raw SQL too, and the
    first encrypted-field test in this codebase lost an afternoon to a raw cursor
    returning nothing because no tenant was pinned.
    """
    own_component(tenant_b, code="B_RAW")

    with tenant_context(tenant_a.pk), connection.cursor() as cursor:
        cursor.execute("SELECT code FROM payroll_component WHERE code = 'B_RAW'")
        assert cursor.fetchall() == []
        cursor.execute("SELECT code FROM payroll_component WHERE code = 'BASIC'")
        assert cursor.fetchall() == [("BASIC",)]


# --------------------------------------------------------------- the constraints


def test_two_tenants_may_use_the_same_component_code(tenant_a, tenant_b):
    own_component(tenant_a, code="TRANSPORT")
    own_component(tenant_b, code="TRANSPORT")  # does not raise


def test_a_tenant_cannot_use_one_code_twice(tenant_a):
    own_component(tenant_a, code="TRANSPORT")
    with pytest.raises(IntegrityError), transaction.atomic():
        own_component(tenant_a, code="TRANSPORT")


def test_the_catalogue_cannot_hold_one_code_twice(source_codes, seeded):
    """NULL = NULL is unknown in PostgreSQL, so a plain unique over (tenant, code)
    would permit exactly this — and every system row has a NULL tenant.

    Sheet 03 says UNIQUE (COALESCE(tenant_id, 0), code) for that reason. It is the
    same trap minimum_wage_rate hit, where it would have let the National Minimum
    Wage be loaded twice.
    """
    with platform_context(), pytest.raises(IntegrityError), transaction.atomic():
        PayrollComponent.objects.create(
            tenant=None,
            code="BASIC",
            name="Basic wage, again",
            component_type=PayrollComponent.ComponentType.EARNING,
            is_system=True,
        )


def test_a_shared_row_must_be_a_system_row(db):
    """Otherwise it is readable by everyone and protected by nothing."""
    with platform_context(), pytest.raises(IntegrityError), transaction.atomic():
        PayrollComponent.objects.create(
            tenant=None,
            code="UNLOCKED",
            name="Shared but not system",
            component_type=PayrollComponent.ComponentType.EARNING,
            is_system=False,
        )


def test_a_tenant_cannot_mint_its_own_system_row(tenant_a):
    """It would be permanently uneditable by the only party that wanted it."""
    with tenant_context(tenant_a.pk), pytest.raises(IntegrityError), transaction.atomic():
        PayrollComponent.objects.create(
            tenant=tenant_a,
            code="MINE_FOREVER",
            name="Locked against myself",
            component_type=PayrollComponent.ComponentType.EARNING,
            is_system=True,
        )


def test_an_unknown_component_type_is_refused(tenant_a):
    with tenant_context(tenant_a.pk), pytest.raises(IntegrityError), transaction.atomic():
        PayrollComponent.objects.create(
            tenant=tenant_a,
            code="ODD",
            name="Neither one thing nor the other",
            component_type="something_else",
        )


def test_components_are_ordered_for_the_payslip(seeded):
    """Earnings, then deductions, then what the employer pays on top."""
    with platform_context():
        ordered = list(PayrollComponent.objects.shared())

    first_deduction = next(
        i
        for i, c in enumerate(ordered)
        if c.component_type == PayrollComponent.ComponentType.DEDUCTION
    )
    last_earning = max(
        i
        for i, c in enumerate(ordered)
        if c.component_type == PayrollComponent.ComponentType.EARNING
    )
    assert last_earning < first_deduction
    assert ordered[-1].component_type == (PayrollComponent.ComponentType.EMPLOYER_CONTRIBUTION)


def test_uif_employer_is_not_a_deduction(seeded):
    """A payslip showing it as one has just understated the employee's net pay."""
    by_code = {c.code: c for c in seeded}
    assert by_code["UIF_ER"].component_type == (
        PayrollComponent.ComponentType.EMPLOYER_CONTRIBUTION
    )
    assert by_code["UIF_EE"].component_type == PayrollComponent.ComponentType.DEDUCTION
    assert by_code["UIF_ER"].sars_source_code_id == by_code["UIF_EE"].sars_source_code_id


# ------------------------------------------------------- the autocommit trap


@pytest.mark.django_db(transaction=True)
def test_seeding_works_outside_a_wrapping_transaction(django_db_setup):
    """THE ONE THE ORDINARY TEST DATABASE CANNOT SEE.

    ``set_config(..., true)`` is TRANSACTION-local, deliberately: a pooled
    connection must not carry one request's tenant into the next. Every test above
    runs inside pytest-django's wrapping transaction, so a context block entered at
    the top of a function is still in force at the bottom.

    A management command has no such wrapper. In autocommit each statement is its
    own transaction, so ``platform_context()`` sets the flag, the statement that
    follows commits, and the flag is gone before the INSERT runs — which then fails
    with "new row violates row-level security policy", naming the policy rather than
    the missing context.

    This test is written with ``transaction=True`` so it runs the way the command
    does. It failed exactly like the command before ``seed_system_components`` took
    out a transaction of its own, and it is the guard for every future context block
    that writes outside a request.
    """
    from django.core.management import call_command

    for code, (taxable, uif, sdl, coida) in CODE_FLAGS.items():
        SarsSourceCode.objects.create(
            code=code,
            description=f"Test code {code}",
            code_group=(
                SarsSourceCode.Group.DEDUCTION
                if code.startswith("4")
                else SarsSourceCode.Group.INCOME
            ),
            is_taxable=taxable,
            is_uif_remuneration=uif,
            is_sdl_remuneration=sdl,
            is_coida_remuneration=coida,
            source_reference="Test fixture",
        )

    call_command("seedcomponents", verbosity=0)

    with platform_context():
        assert PayrollComponent.objects.shared().count() == len(SYSTEM_COMPONENTS)

    # And still idempotent when the wrapper is not there to hide a second write.
    call_command("seedcomponents", verbosity=0)
    with platform_context():
        assert PayrollComponent.objects.shared().count() == len(SYSTEM_COMPONENTS)
