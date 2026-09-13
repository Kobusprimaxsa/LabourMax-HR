"""Public holidays, SARS source codes and banks.

Three small tables that each stop a specific recurring mistake:

``public_holiday``
    Stored per date, never derived from a rule. The Public Holidays Act moves a
    Sunday holiday to the following Monday, and that shift is recorded as the Monday
    row pointing back at the Sunday — because the shift affects pay on a specific
    date and a re-run in 2030 must reproduce it exactly. Easter moves; election days
    and days of mourning are proclaimed and follow no rule at all.

``sars_source_code``
    Four separate remuneration-base flags rather than one ``is_taxable``, because the
    bases genuinely differ. Commission is excluded from the UIF contribution base
    while bonuses are not, and a single flag gets that wrong silently.

``bank``
    Not a cited statutory model. A universal branch code is published by the bank,
    not gazetted, and demanding a statutory citation for one invites a fabricated
    citation.
"""

from __future__ import annotations

import datetime

import pytest
from django.db import IntegrityError, transaction

from statutory.models import Bank, BankBranch, PublicHoliday, SarsSourceCode

CITATION = "Test fixture, not a real proclamation"


def holiday(*, on, name="Test holiday", shifted_from=None, statutory=True):
    return PublicHoliday.objects.create(
        holiday_date=on,
        name=name,
        is_statutory=statutory,
        shifted_from_date=shifted_from,
        source_reference=CITATION,
    )


# ------------------------------------------------------------- public holidays


@pytest.mark.statutory
def test_a_holiday_without_a_citation_is_refused(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        PublicHoliday.objects.create(
            holiday_date=datetime.date(2026, 4, 27),
            name="Freedom Day",
            source_reference="",
        )


@pytest.mark.statutory
def test_one_holiday_per_date_per_country(db):
    holiday(on=datetime.date(2026, 4, 27), name="Freedom Day")
    with pytest.raises(IntegrityError), transaction.atomic():
        holiday(on=datetime.date(2026, 4, 27), name="Freedom Day loaded twice")


@pytest.mark.statutory
def test_a_shifted_holiday_stores_the_sunday_it_moved_from(db):
    """The Act's Monday rule, recorded rather than computed.

    A payroll re-run years later reads the stored Monday. Nothing recalculates the
    shift, so nothing can recalculate it differently.
    """
    sunday = datetime.date(2026, 4, 26)
    monday = datetime.date(2026, 4, 27)
    row = holiday(on=monday, name="Freedom Day observed", shifted_from=sunday)

    assert row.shifted_from_date == sunday
    assert row.holiday_date.weekday() == 0


@pytest.mark.statutory
def test_a_shift_forwards_in_time_is_refused(db):
    """The Act moves a holiday to the following Monday. A shift the other way is a
    data entry error, and it would show up as a holiday appearing on the wrong side
    of a pay period boundary."""
    with pytest.raises(IntegrityError), transaction.atomic():
        holiday(
            on=datetime.date(2026, 4, 26),
            shifted_from=datetime.date(2026, 4, 27),
        )


@pytest.mark.statutory
def test_a_proclaimed_once_off_day_is_not_statutory(db):
    """Election days and days of mourning. Paid like a holiday, recurring never."""
    row = holiday(on=datetime.date(2026, 11, 4), name="Municipal elections", statutory=False)
    assert row.is_statutory is False


@pytest.mark.statutory
def test_holidays_come_back_in_date_order(db):
    holiday(on=datetime.date(2026, 12, 16), name="Day of Reconciliation")
    holiday(on=datetime.date(2026, 3, 21), name="Human Rights Day")
    holiday(on=datetime.date(2026, 6, 16), name="Youth Day")

    assert [h.holiday_date.month for h in PublicHoliday.objects.all()] == [3, 6, 12]


# ----------------------------------------------------------- SARS source codes


@pytest.mark.statutory
def test_a_source_code_is_unique(db):
    SarsSourceCode.objects.create(
        code="3601",
        description="Income (PAYE)",
        code_group=SarsSourceCode.Group.INCOME,
        source_reference=CITATION,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        SarsSourceCode.objects.create(
            code="3601",
            description="Loaded twice",
            code_group=SarsSourceCode.Group.INCOME,
            source_reference=CITATION,
        )


@pytest.mark.statutory
def test_the_four_remuneration_bases_are_independent(db):
    """The reason there are four columns rather than one ``is_taxable``.

    Commission is taxable and is NOT part of the UIF contribution base. One flag
    would make that impossible to express, and the error would be invisible until a
    UI-19 reconciliation.
    """
    commission = SarsSourceCode.objects.create(
        code="3606",
        description="Commission",
        code_group=SarsSourceCode.Group.INCOME,
        is_taxable=True,
        is_uif_remuneration=False,
        is_sdl_remuneration=True,
        is_coida_remuneration=True,
        source_reference=CITATION,
    )
    commission.refresh_from_db()
    assert commission.is_taxable is True
    assert commission.is_uif_remuneration is False


@pytest.mark.statutory
def test_a_code_may_be_bounded_by_tax_year_rather_than_by_date(db):
    """SARS retires codes at tax-year boundaries, and a retired code must stay usable
    for reprints of the years it was valid in."""
    code = SarsSourceCode.objects.create(
        code="3699",
        description="Gross remuneration",
        code_group=SarsSourceCode.Group.TOTAL,
        source_reference=CITATION,
    )
    assert code.valid_from_tax_year is None
    assert code.valid_to_tax_year is None


# -------------------------------------------------------------------- banking


@pytest.mark.statutory
def test_account_length_bounds_must_be_ordered(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        Bank.objects.create(
            name="Backwards Bank",
            account_number_min_length=11,
            account_number_max_length=7,
        )


@pytest.mark.statutory
def test_equal_length_bounds_are_allowed(db):
    """A bank with a fixed account length. The constraint is >=, not >."""
    bank = Bank.objects.create(
        name="Fixed Length Bank",
        account_number_min_length=11,
        account_number_max_length=11,
    )
    assert bank.pk


@pytest.mark.statutory
def test_a_branch_code_is_unique_within_a_bank_but_not_across_banks(db):
    first = Bank.objects.create(
        name="First Test Bank", account_number_min_length=7, account_number_max_length=11
    )
    second = Bank.objects.create(
        name="Second Test Bank", account_number_min_length=7, account_number_max_length=11
    )

    BankBranch.objects.create(bank=first, branch_code="000000", branch_name="Head office")
    assert BankBranch.objects.create(
        bank=second, branch_code="000000", branch_name="Head office"
    ).pk

    with pytest.raises(IntegrityError), transaction.atomic():
        BankBranch.objects.create(bank=first, branch_code="000000", branch_name="Duplicate")


@pytest.mark.statutory
def test_a_bank_with_branches_cannot_be_deleted(db):
    from django.db.models import ProtectedError

    bank = Bank.objects.create(
        name="Protected Bank", account_number_min_length=7, account_number_max_length=11
    )
    BankBranch.objects.create(bank=bank, branch_code="123456", branch_name="Branch")

    with pytest.raises(ProtectedError):
        bank.delete()


@pytest.mark.statutory
def test_a_source_code_without_a_citation_is_refused(db):
    """The four base flags are compliance decisions, so the row must say where they
    came from. Added when the table became a cited one - see D-69."""
    with pytest.raises(IntegrityError), transaction.atomic():
        SarsSourceCode.objects.create(
            code="3607",
            description="Overtime",
            code_group=SarsSourceCode.Group.INCOME,
            source_reference="",
        )
