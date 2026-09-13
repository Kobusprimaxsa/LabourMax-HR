"""Encryption at rest, and the three ways it quietly fails to protect anything.

1. **A missing key falls back to plaintext.** Every test asserting "the value round
   trips" still passes, because it does — in the clear. The field refuses instead.
2. **An encrypted column is searched anyway.** Fernet is non-deterministic, so the
   query matches nothing and reads as "no such record". The field refuses the lookup.
3. **The ciphertext is never actually checked.** A test that writes and reads through
   the ORM passes whether or not anything was encrypted. So the test below reads the
   raw column with a second cursor and asserts the plaintext is not in it.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.db import connection

from core.db.fields import keyed_hash, last4, normalise
from core.models import Tenant
from employers.models import Employer, EmployerBankAccount
from statutory.models import Bank, Sector

ACCOUNT = "1234567890"


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(trading_name="Test tenant")


@pytest.fixture
def employer(db, tenant):
    from core.managers import tenant_context

    sector = Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    with tenant_context(tenant.pk):
        return Employer.objects.create(tenant=tenant, trading_name="A Household", sector=sector)


@pytest.fixture
def bank(db):
    return Bank.objects.create(name="Test Bank", universal_branch_code="632005")


# ------------------------------------------------------------------ the basics


@pytest.mark.django_db
def test_normalising_makes_two_spellings_of_one_account_equal():
    assert normalise("1234 5678 90") == normalise("1234567890")
    assert normalise("gb-12-ab") == "GB12AB"


@pytest.mark.django_db
def test_the_hash_is_keyed_and_stable(settings):
    settings.FIELD_ENCRYPTION_KEY = "S0m3-t3st-k3y-0f-th3-right-l3ngth-AAAAAAAAAA="
    first = keyed_hash("1234 5678 90")
    second = keyed_hash("1234567890")
    assert first == second, "Two spellings of one account must hash alike."
    assert first != keyed_hash("1234567891")
    assert len(first) == 64


@pytest.mark.django_db
def test_last4_is_what_a_person_recognises():
    assert last4("1234 5678 90") == "7890"


# -------------------------------------------------- the value is actually hidden


@pytest.mark.django_db
def test_the_account_number_is_not_in_the_database_in_the_clear(employer, bank, tenant):
    """Read the raw column with a second cursor.

    A round-trip through the ORM proves nothing: it passes identically whether the
    value was encrypted or stored as typed.
    """
    from core.managers import tenant_context

    with tenant_context(tenant.pk):
        account = EmployerBankAccount.objects.create(
            tenant=tenant,
            employer=employer,
            bank=bank,
            branch_code="632005",
            account_holder="A Household",
            account_number=ACCOUNT,
            effective_from="2026-03-01",
        )

    # The raw read has to be inside the tenant context too: row-level security
    # applies to a cursor exactly as it applies to the ORM, so an unpinned session
    # gets no rows rather than an error. Forgetting this reads as "the row is not
    # there" - the failure mode CLAUDE.md warns about, met here in a test.
    with tenant_context(tenant.pk), connection.cursor() as cursor:
        cursor.execute(
            "SELECT account_number FROM employer_bank_account WHERE id = %s", [account.pk]
        )
        stored = cursor.fetchone()[0]

    assert ACCOUNT not in stored, "The account number is sitting in the database in the clear."
    assert stored.startswith("gAAAAA"), "Not a Fernet token."


@pytest.mark.django_db
def test_it_round_trips_through_the_orm(employer, bank, tenant):
    from core.managers import tenant_context

    with tenant_context(tenant.pk):
        created = EmployerBankAccount.objects.create(
            tenant=tenant,
            employer=employer,
            bank=bank,
            branch_code="632005",
            account_holder="A Household",
            account_number=ACCOUNT,
            effective_from="2026-03-01",
        )
        reloaded = EmployerBankAccount.objects.get(pk=created.pk)

    assert reloaded.account_number == ACCOUNT
    assert reloaded.account_number_last4 == "7890"


@pytest.mark.django_db
def test_two_rows_with_the_same_account_have_different_ciphertext(employer, bank, tenant):
    """Which is the point, and also the reason the field cannot be searched.

    The second row is not primary: only one live primary account per employer is
    allowed, and a payment file generated against two would pick one by row order.
    """
    from core.managers import tenant_context

    with tenant_context(tenant.pk):
        first = EmployerBankAccount.objects.create(
            tenant=tenant,
            employer=employer,
            bank=bank,
            branch_code="632005",
            account_holder="A Household",
            account_number=ACCOUNT,
            effective_from="2026-03-01",
        )
        second = EmployerBankAccount.objects.create(
            tenant=tenant,
            employer=employer,
            bank=bank,
            branch_code="632005",
            account_holder="A Household",
            account_number=ACCOUNT,
            effective_from="2027-03-01",
            is_primary=False,
        )

    with tenant_context(tenant.pk), connection.cursor() as cursor:
        cursor.execute(
            "SELECT account_number FROM employer_bank_account WHERE id IN (%s, %s)",
            [first.pk, second.pk],
        )
        stored = [row[0] for row in cursor.fetchall()]

    assert stored[0] != stored[1]
    assert first.account_number_hash == second.account_number_hash, (
        "The hash column is what makes the duplicate findable."
    )


# ------------------------------------------------------------- the two refusals


@pytest.mark.django_db
def test_searching_an_encrypted_column_is_refused_rather_than_matching_nothing(db):
    """The failure guarded against returns an empty queryset, not an error.

    An empty result reads as "no such account", which is how a duplicate check comes
    to report that a bank account has never been seen before.
    """
    with pytest.raises(FieldError) as caught:
        list(EmployerBankAccount.objects.filter(account_number=ACCOUNT))

    assert "_hash" in str(caught.value), "The message must name the column to use instead."


@pytest.mark.django_db
def test_a_missing_key_refuses_the_write_rather_than_storing_plaintext(
    settings, employer, bank, tenant
):
    from core.managers import tenant_context

    settings.FIELD_ENCRYPTION_KEY = ""

    with pytest.raises(ImproperlyConfigured), tenant_context(tenant.pk):
        EmployerBankAccount.objects.create(
            tenant=tenant,
            employer=employer,
            bank=bank,
            branch_code="632005",
            account_holder="A Household",
            account_number=ACCOUNT,
            effective_from="2026-03-01",
        )


@pytest.mark.django_db
def test_a_value_longer_than_the_field_accepts_is_refused(employer, bank, tenant):
    from core.managers import tenant_context

    with pytest.raises(ValueError), tenant_context(tenant.pk):
        EmployerBankAccount.objects.create(
            tenant=tenant,
            employer=employer,
            bank=bank,
            branch_code="632005",
            account_holder="A Household",
            account_number="9" * 200,
            effective_from="2026-03-01",
        )
