"""The employee record — identity, the duplicate guard, and the sort columns.

The tests worth reading are the ones where a plausible capture produces a wrong
answer that nothing downstream would catch:

- the same person captured twice for one employer, which the hash must refuse,
- the same person legitimately employed by two subscribers, which it must allow,
- and the hash itself, which must not be equal across tenants even for the same
  number, because equality there is a fact leaking over the tenant boundary that
  no isolation test can see.
"""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from core.db.fields import keyed_hash
from core.managers import tenant_context
from core.models import Tenant
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeAddress, EmployeeContact, next_employee_number
from employers.models import Employer
from statutory.models import Sector

pytestmark = pytest.mark.django_db

BORN = datetime.date(1990, 1, 1)


def make_id(yymmdd="900101", sequence="5009", citizenship="0", race="8"):
    body = f"{yymmdd}{sequence}{citizenship}{race}"
    return body + str(luhn_check_digit(body))


@pytest.fixture
def sector(db):
    return Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(trading_name="Subscriber A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(trading_name="Subscriber B")


@pytest.fixture
def employer_a(db, tenant_a, sector):
    with tenant_context(tenant_a.pk):
        return Employer.objects.create(tenant=tenant_a, trading_name="Household A", sector=sector)


@pytest.fixture
def employer_b(db, tenant_b, sector):
    with tenant_context(tenant_b.pk):
        return Employer.objects.create(tenant=tenant_b, trading_name="Household B", sector=sector)


def make_employee(tenant, employer, **overrides):
    values = {
        "first_name": "Thandi",
        "last_name": "Mokoena",
        "date_of_birth": BORN,
        "mobile_number": "+27820000001",
        "email": "thandi@example.com",
        "id_number": make_id(),
        **overrides,
    }
    with tenant_context(tenant.pk):
        return Employee.objects.create(tenant=tenant, employer=employer, **values)


# ------------------------------------------------------------------- the basics


def test_an_employee_is_captured_with_the_companion_columns_filled(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a)

    assert employee.id_number_last4 == make_id()[-4:]
    assert len(employee.id_number_hash) == 64
    assert employee.id_number_verified is True
    assert employee.employee_number == "EMP0001"


def test_the_id_number_is_not_in_the_database_in_the_clear(tenant_a, employer_a):
    """Invariant: a database dump must not hand over ID numbers.

    Read through a raw cursor INSIDE a tenant context — row-level security applies
    to raw SQL exactly as it does to the ORM, so an unpinned cursor returns nothing
    and the test would pass by examining zero rows.
    """
    employee = make_employee(tenant_a, employer_a)
    plaintext = make_id()

    with tenant_context(tenant_a.pk), connection.cursor() as cursor:
        cursor.execute("SELECT id_number FROM employee WHERE id = %s", [employee.pk])
        rows = cursor.fetchall()

    assert rows, "The cursor saw no rows - check the tenant context, not the encryption."
    stored = rows[0][0]
    assert plaintext not in stored
    assert stored != plaintext


def test_the_email_is_stored_lower_cased(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a, email="Thandi.M@Example.COM")
    assert employee.email == "thandi.m@example.com"


# -------------------------------------------------------------- the duplicate guard


def test_the_same_person_cannot_be_captured_twice_for_one_employer(tenant_a, employer_a):
    make_employee(tenant_a, employer_a)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_employee(tenant_a, employer_a, first_name="Thandiwe", email="t2@example.com")


def test_two_subscribers_may_each_employ_the_same_person(
    tenant_a, tenant_b, employer_a, employer_b
):
    """Not an edge case — it is the ordinary shape of this market.

    A domestic worker with two households is two employment relationships with two
    unrelated subscribers. A global unique on the ID number would make the second
    employer unable to capture their own employee, and would tell each of them
    something about the other.
    """
    make_employee(tenant_a, employer_a)
    make_employee(tenant_b, employer_b, email="thandi2@example.com")

    with tenant_context(tenant_a.pk):
        assert Employee.objects.count() == 1
    with tenant_context(tenant_b.pk):
        assert Employee.objects.count() == 1


def test_the_hash_of_one_number_differs_between_tenants(tenant_a, tenant_b, employer_a, employer_b):
    """THE LEAK NO ISOLATION TEST CAN SEE.

    An unscoped HMAC gives the same digest for the same person everywhere, so a
    support engineer, a read replica or a backup can join two subscribers' employee
    tables on the hash and learn that one household's domestic worker also works for
    another. Nothing decrypts, no policy is bypassed, and no forbidden row is read —
    which is exactly why the generated isolation suite cannot catch it and this test
    has to exist (D-95).
    """
    first = make_employee(tenant_a, employer_a)
    second = make_employee(tenant_b, employer_b, email="thandi2@example.com")

    assert first.id_number_hash != second.id_number_hash


def test_the_scope_is_the_tenant_and_nothing_else(tenant_a, employer_a):
    """Pins the scope string, so a refactor cannot quietly change what equality means.

    If this ever fails because the scope was changed, every stored hash is stale and
    the duplicate guard silently stops matching — it does not error, it just reports
    that everybody is new.
    """
    employee = make_employee(tenant_a, employer_a)
    assert employee.id_number_hash == keyed_hash(make_id(), scope=f"tenant:{tenant_a.pk}")


# ------------------------------------------------------------------- validation


def test_a_number_whose_checksum_fails_is_refused_with_the_reason(tenant_a, employer_a):
    broken = make_id()[:-1] + str((int(make_id()[-1]) + 1) % 10)
    employee = Employee(
        tenant=tenant_a,
        employer=employer_a,
        first_name="A",
        last_name="B",
        date_of_birth=BORN,
        mobile_number="+27820000009",
        email="a@example.com",
        id_number=broken,
    )
    with pytest.raises(ValidationError) as caught:
        employee.full_clean(exclude=["tenant", "sort_name_first", "sort_name_last"])
    assert "check digit" in str(caught.value)


def test_a_date_of_birth_contradicting_the_id_number_is_refused(tenant_a, employer_a):
    """Both fields are individually plausible. Only comparing them finds it."""
    employee = Employee(
        tenant=tenant_a,
        employer=employer_a,
        first_name="A",
        last_name="B",
        date_of_birth=datetime.date(1990, 1, 2),
        mobile_number="+27820000010",
        email="b@example.com",
        id_number=make_id(),
    )
    with pytest.raises(ValidationError) as caught:
        employee.full_clean(exclude=["tenant", "sort_name_first", "sort_name_last"])
    assert "01 January 1990" in str(caught.value)
    assert "will reach SARS" in str(caught.value)


def test_a_passport_is_accepted_and_left_unverified(tenant_a, employer_a):
    """An employer holding a passport must still be able to run payroll.

    ``id_number_verified`` false means "nobody has verified this", not "this is
    wrong" — there is no checksum on a passport for this system to check.
    """
    employee = make_employee(
        tenant_a,
        employer_a,
        id_type=Employee.IdType.PASSPORT,
        id_number="A01234567",
        email="p@example.com",
    )
    assert employee.id_number_verified is False
    assert employee.id_number_hash != ""


def test_an_employee_with_no_email_must_say_so(tenant_a, employer_a):
    employee = Employee(
        tenant=tenant_a,
        employer=employer_a,
        first_name="A",
        last_name="B",
        date_of_birth=BORN,
        mobile_number="+27820000011",
        email="",
        id_number=make_id(),
    )
    with pytest.raises(ValidationError) as caught:
        employee.full_clean(exclude=["tenant", "sort_name_first", "sort_name_last"])
    assert "no email address" in str(caught.value)


def test_the_no_email_flag_lets_the_capture_through(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a, email="", has_no_email=True)
    assert employee.pk is not None


def test_claiming_no_email_while_giving_one_is_refused(tenant_a, employer_a):
    employee = Employee(
        tenant=tenant_a,
        employer=employer_a,
        first_name="A",
        last_name="B",
        date_of_birth=BORN,
        mobile_number="+27820000012",
        email="c@example.com",
        has_no_email=True,
        id_number=make_id(),
    )
    with pytest.raises(ValidationError):
        employee.full_clean(exclude=["tenant", "sort_name_first", "sort_name_last"])


def test_the_database_refuses_a_birth_date_in_the_future(tenant_a, employer_a):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_employee(tenant_a, employer_a, date_of_birth=datetime.date(2099, 1, 1))


# --------------------------------------------------------------- employee numbers


def test_numbers_are_generated_in_sequence_per_employer(tenant_a, employer_a):
    first = make_employee(tenant_a, employer_a)
    second = make_employee(
        tenant_a, employer_a, id_number=make_id(sequence="5010"), email="d@example.com"
    )
    assert (first.employee_number, second.employee_number) == ("EMP0001", "EMP0002")


def test_a_typed_number_is_kept_and_does_not_disturb_generation(tenant_a, employer_a):
    """An employer migrating from paper types in the numbers they already use."""
    make_employee(tenant_a, employer_a, employee_number="CLEANER-7")
    nxt = make_employee(
        tenant_a, employer_a, id_number=make_id(sequence="5011"), email="e@example.com"
    )
    assert nxt.employee_number == "EMP0001", (
        "A number that is not in the EMP#### shape must not be read as one."
    )


def test_one_employer_cannot_use_a_number_twice(tenant_a, employer_a):
    make_employee(tenant_a, employer_a, employee_number="X1")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_employee(
            tenant_a,
            employer_a,
            employee_number="X1",
            id_number=make_id(sequence="5012"),
            email="f@example.com",
        )


def test_generation_with_no_tenant_pinned_returns_the_first_number(tenant_a, employer_a):
    """Row-level security returns no rows when no tenant is pinned, so the scan
    behind this sees nothing and starts again at one.

    Recorded rather than fixed here: the unique constraint turns it into a visible
    IntegrityError rather than a duplicate, which is the right place for the
    guarantee. Every caller in the application arrives with a tenant pinned.
    """
    make_employee(tenant_a, employer_a)
    with tenant_context(None):
        assert next_employee_number(employer_a.pk) == "EMP0001"


# ------------------------------------------------------------- the sort columns


def test_the_sort_columns_are_generated_and_lower_cased(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a, first_name="Thandi", last_name="Mokoena")
    with tenant_context(tenant_a.pk):
        employee.refresh_from_db()
    assert employee.sort_name_first == "thandi mokoena"
    assert employee.sort_name_last == "mokoena thandi"


def test_the_sort_columns_follow_the_name_without_being_written(tenant_a, employer_a):
    """Both statements are inside the tenant context, and the reason is worth the line.

    Written the obvious way first, this test failed with "new row violates row-level
    security policy" on an INSERT - for a row that already existed and was only being
    renamed. That is a variation on the trap in CLAUDE.md that was not yet written
    down: with no tenant pinned, Django's UPDATE matches zero rows, and Django reads
    zero affected rows as "this row is not in the table yet" and falls back to an
    INSERT with the primary key set. The message names the policy and the insert,
    and mentions neither the update nor the missing context.
    """
    employee = make_employee(tenant_a, employer_a)
    with tenant_context(tenant_a.pk):
        employee.last_name = "Ndlovu"
        employee.save()
        employee.refresh_from_db()
    assert employee.sort_name_last == "ndlovu thandi"


def test_accented_names_sort_where_a_reader_expects(tenant_a, employer_a):
    """What the ICU collation buys, and the reason it is set in the creating
    migration rather than added later (D-17).

    Under the C collation every accented letter sorts after every unaccented one,
    so Étienne lands after Zulu — at the bottom of the list, where the employer
    stops looking.
    """
    for index, (first, last) in enumerate(
        [("Anna", "Abrahams"), ("Étienne", "Bekker"), ("Zola", "Cele")]
    ):
        make_employee(
            tenant_a,
            employer_a,
            first_name=first,
            last_name=last,
            id_number=make_id(sequence=f"50{index:02d}"),
            email=f"{index}@example.com",
        )

    with tenant_context(tenant_a.pk):
        ordered = list(
            Employee.objects.order_by("sort_name_last").values_list("last_name", flat=True)
        )
    assert ordered == ["Abrahams", "Bekker", "Cele"]


# ------------------------------------------------------------ addresses and contacts


def test_an_address_is_effective_dated(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a)
    with tenant_context(tenant_a.pk):
        address = EmployeeAddress.objects.create(
            tenant=tenant_a,
            employee=employee,
            line1="12 Main Road",
            city="Cape Town",
            effective_from=datetime.date(2026, 3, 1),
        )
    assert address.effective_to is None


def test_an_address_period_cannot_end_before_it_starts(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a)
    with pytest.raises(IntegrityError), transaction.atomic(), tenant_context(tenant_a.pk):
        EmployeeAddress.objects.create(
            tenant=tenant_a,
            employee=employee,
            line1="12 Main Road",
            city="Cape Town",
            effective_from=datetime.date(2026, 3, 1),
            effective_to=datetime.date(2026, 2, 1),
        )


def test_only_one_primary_contact_per_type(tenant_a, employer_a):
    """Two primary emergency contacts means whoever reads the record picks one,
    at the moment somebody needs it."""
    employee = make_employee(tenant_a, employer_a)
    with tenant_context(tenant_a.pk):
        EmployeeContact.objects.create(
            tenant=tenant_a,
            employee=employee,
            full_name="Sipho Mokoena",
            phone="+27820000100",
            is_primary=True,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            EmployeeContact.objects.create(
                tenant=tenant_a,
                employee=employee,
                full_name="Nomsa Mokoena",
                phone="+27820000101",
                is_primary=True,
            )


def test_a_second_non_primary_contact_of_the_same_type_is_fine(tenant_a, employer_a):
    employee = make_employee(tenant_a, employer_a)
    with tenant_context(tenant_a.pk):
        for name in ("Sipho", "Nomsa"):
            EmployeeContact.objects.create(
                tenant=tenant_a, employee=employee, full_name=name, phone="+27820000102"
            )
        assert EmployeeContact.objects.filter(employee=employee).count() == 2


# ---------------------------------------------------------------------- caches


def test_the_cache_columns_start_null_rather_than_guessing(tenant_a, employer_a):
    """NULL means nobody has computed it, not 'no pay group' (D-18).

    The maintenance job arrives with employee_remuneration. Until then an honest
    NULL is right, and a default of anything else would be a computed-looking value
    that nothing computed.
    """
    employee = make_employee(tenant_a, employer_a)
    assert employee.current_pay_group_id is None
    assert employee.current_pay_basis == ""
