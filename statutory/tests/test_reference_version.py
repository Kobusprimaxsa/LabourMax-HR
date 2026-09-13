"""Reference versions, verification, and the staleness guard.

The guard is the reason this phase is three weeks rather than three days. Every
other test in the product checks that a calculation is right; these check that the
system refuses to calculate when it does not know whether it is right.
"""

from __future__ import annotations

import datetime

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from statutory.models import ReferenceDataVersion, StatutoryWatchItem
from statutory.staleness import (
    ReferenceDataStaleError,
    assert_reference_data_covers,
    current_version,
)

MARCH_2026 = datetime.date(2026, 3, 1)
FEB_2027 = datetime.date(2027, 2, 28)


def make_version(
    label="REF-2026.03.01",
    *,
    applies_from=MARCH_2026,
    verified=True,
    golden=True,
    current_through=FEB_2027,
):
    version = ReferenceDataVersion(
        version_label=label,
        applies_from=applies_from,
        golden_tests_passed=golden,
    )
    if verified:
        from core.models import AppUser

        checker, _ = AppUser.objects.get_or_create(
            email="checker@labourmax.test",
            defaults={"mobile_number": "+27820009001"},
        )
        version.verified_by_user = checker
        version.verified_at = timezone.now()
        version.data_current_through = current_through
    version.save()
    return version


# ------------------------------------------------------------------ constraints


@pytest.mark.statutory
def test_data_current_through_cannot_be_set_without_verification(db):
    """The workbook's rule, enforced rather than documented.

    The load is the moment someone is in a hurry, which is exactly when a
    "confirmed correct through" date would get filled in without anyone having
    confirmed anything.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        ReferenceDataVersion.objects.create(
            version_label="REF-UNVERIFIED",
            applies_from=MARCH_2026,
            data_current_through=FEB_2027,
        )


@pytest.mark.statutory
def test_verified_by_and_verified_at_must_travel_together(db):
    with pytest.raises(IntegrityError), transaction.atomic():
        ReferenceDataVersion.objects.create(
            version_label="REF-HALF-VERIFIED",
            applies_from=MARCH_2026,
            verified_at=timezone.now(),
        )


@pytest.mark.statutory
def test_data_current_through_cannot_precede_applies_from(db):
    from core.models import AppUser

    checker = AppUser.objects.create_user(
        email="backwards@labourmax.test", password="x", mobile_number="+27820009002"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ReferenceDataVersion.objects.create(
            version_label="REF-BACKWARDS",
            applies_from=MARCH_2026,
            verified_by_user=checker,
            verified_at=timezone.now(),
            data_current_through=datetime.date(2026, 2, 1),
        )


# ------------------------------------------------------- which version is in force


@pytest.mark.statutory
def test_an_unverified_version_is_never_in_force(db):
    """Activation is derived, so there is no boolean to flip at 17:55 on 28 February."""
    make_version("REF-UNCHECKED", verified=False, golden=True)
    assert current_version(MARCH_2026) is None


@pytest.mark.statutory
def test_a_version_failing_golden_tests_is_never_in_force(db):
    """A version that cannot reproduce SARS's own worked examples does not go live."""
    make_version("REF-FAILS-GOLDEN", golden=False)
    assert current_version(MARCH_2026) is None


@pytest.mark.statutory
def test_the_newest_applicable_version_wins(db):
    make_version("REF-2026.03.01", applies_from=MARCH_2026)
    newer = make_version(
        "REF-2026.09.01",
        applies_from=datetime.date(2026, 9, 1),
        current_through=datetime.date(2027, 2, 28),
    )

    assert current_version(datetime.date(2026, 10, 1)) == newer
    # A date before the newer version existed must still resolve to the older one.
    assert current_version(datetime.date(2026, 4, 1)).version_label == "REF-2026.03.01"


# ---------------------------------------------------------------- the guard itself


@pytest.mark.statutory
def test_a_run_inside_the_confirmed_window_is_allowed(db):
    version = make_version()
    assert assert_reference_data_covers(datetime.date(2026, 6, 30)) == version


@pytest.mark.statutory
def test_a_run_past_the_confirmed_date_is_blocked(db):
    """The core failure this phase exists to prevent.

    The effective-dated tables would happily return February 2026's rates for a
    period in 2028, because that row is still the newest with no end date. Correct
    for the table, wrong for a payslip.
    """
    make_version()
    with pytest.raises(ReferenceDataStaleError, match="only confirmed correct to"):
        assert_reference_data_covers(datetime.date(2027, 3, 31))


@pytest.mark.statutory
def test_a_run_with_no_reference_data_at_all_is_blocked(db):
    with pytest.raises(ReferenceDataStaleError, match="no verified statutory reference data"):
        assert_reference_data_covers(datetime.date(2026, 6, 30))


@pytest.mark.statutory
def test_the_block_names_the_dates_an_employer_can_act_on(db):
    """The person who hits this cannot fix it, so the message must say who can."""
    make_version()
    with pytest.raises(ReferenceDataStaleError) as caught:
        assert_reference_data_covers(datetime.date(2027, 4, 30))

    message = str(caught.value)
    assert "30 April 2027" in message
    assert "28 February 2027" in message
    assert "Labourmax" in message


@pytest.mark.statutory
def test_the_guard_labels_what_is_being_blocked(db):
    make_version()
    with pytest.raises(ReferenceDataStaleError, match="The March 2027 run for Acme Cleaning"):
        assert_reference_data_covers(
            datetime.date(2027, 3, 31), what="The March 2027 run for Acme Cleaning"
        )


# ------------------------------------------------------------------ watch calendar


@pytest.mark.statutory
def test_a_no_change_confirmation_still_records_that_someone_looked(db):
    """Without this there is no way to tell "did not move" from "nobody checked"."""
    item = StatutoryWatchItem.objects.create(
        watch_code="UIF_CEILING",
        name="UIF contribution ceiling",
        change_cadence=StatutoryWatchItem.Cadence.IRREGULAR,
        source_name="Department of Employment and Labour ministerial notice",
    )
    assert item.last_confirmed_date is None

    item.confirm_checked(on_date=datetime.date(2026, 10, 1))

    item.refresh_from_db()
    assert item.last_confirmed_date == datetime.date(2026, 10, 1)
    assert item.status == StatutoryWatchItem.Status.CURRENT
    assert item.last_change_effective_date is None


@pytest.mark.statutory
def test_a_published_change_is_flagged_until_it_is_loaded(db):
    item = StatutoryWatchItem.objects.create(
        watch_code="NMW_ANNUAL",
        name="National minimum wage",
        source_name="Department of Employment and Labour gazette",
    )
    item.confirm_checked(
        on_date=datetime.date(2027, 2, 10), changed_effective_from=datetime.date(2027, 3, 1)
    )

    item.refresh_from_db()
    assert item.status == StatutoryWatchItem.Status.CHANGE_PUBLISHED_NOT_LOADED
    assert item.last_change_effective_date == datetime.date(2027, 3, 1)


@pytest.mark.statutory
def test_the_uif_ceiling_is_not_on_an_annual_cadence(db):
    """It moves on ministerial notice, on no fixed calendar.

    It is the parameter most often missed in South African payroll precisely
    because everything else moves on 1 March and this does not.
    """
    item = StatutoryWatchItem.objects.create(
        watch_code="UIF_MONTHLY_CEILING",
        name="UIF contribution ceiling",
        change_cadence=StatutoryWatchItem.Cadence.IRREGULAR,
        source_name="Department of Employment and Labour ministerial notice",
    )
    assert item.change_cadence != StatutoryWatchItem.Cadence.ANNUAL_1_MARCH
    assert item.next_expected_date is None
