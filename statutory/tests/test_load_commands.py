"""The two management commands, and the rule that keeps them two commands.

Loading and verifying are separate acts by separate people. The database can enforce
the *ordering* — ``data_current_through`` cannot be set without ``verified_at`` — but
it cannot tell which of two user ids was holding the gazette. That check lives in the
command, so it is tested here.
"""

from __future__ import annotations

import datetime
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import AppUser
from statutory.models import ReferenceDataVersion

CITATION = "Test fixture, not a real gazette"


@pytest.fixture
def fixture_file(tmp_path):
    document = {
        "version_label": "REF-TEST-CMD",
        "applies_from": "2026-03-01",
        "tables": {
            "statutory_parameter": [
                {
                    "parameter_code": "TEST_PARAMETER",
                    "value_numeric": "1.000000",
                    "unit": "percent",
                    "effective_from": "2026-03-01",
                    "source_reference": CITATION,
                }
            ]
        },
    }
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def loader_user(db):
    return AppUser.objects.create_user(email="loader@example.com", password="x" * 16)


@pytest.fixture
def verifier_user(db):
    return AppUser.objects.create_user(email="verifier@example.com", password="x" * 16)


@pytest.mark.statutory
def test_loadstatutory_loads_and_records_who_loaded_it(db, fixture_file, loader_user):
    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader_user.email)

    version = ReferenceDataVersion.objects.get(version_label="REF-TEST-CMD")
    assert version.loaded_by_user == loader_user
    assert version.verified_at is None


@pytest.mark.statutory
def test_loadstatutory_refuses_an_unknown_loader(db, fixture_file):
    with pytest.raises(CommandError):
        call_command("loadstatutory", str(fixture_file), "--loaded-by", "nobody@example.com")
    assert ReferenceDataVersion.objects.count() == 0


@pytest.mark.statutory
def test_loadstatutory_reports_a_refusal_as_a_command_error(db, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "version_label": "REF-BAD",
                "applies_from": "2026-03-01",
                "tables": {
                    "statutory_parameter": [
                        {
                            "parameter_code": "TEST_PARAMETER",
                            "value_numeric": "1.000000",
                            "effective_from": "2026-03-01",
                            "source_reference": "",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError) as caught:
        call_command("loadstatutory", str(path))

    assert "Nothing was written" in str(caught.value)
    assert ReferenceDataVersion.objects.count() == 0


@pytest.mark.statutory
def test_the_loader_cannot_verify_their_own_load(db, fixture_file, loader_user):
    """Verification is a second reading by a second pair of eyes, or it is nothing."""
    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader_user.email)

    with pytest.raises(CommandError) as caught:
        call_command(
            "verifystatutory",
            "REF-TEST-CMD",
            "--verified-by",
            loader_user.email,
            "--current-through",
            "2027-02-28",
            "--golden-tests-passed",
        )

    assert "cannot verify" in str(caught.value)
    assert ReferenceDataVersion.objects.get().verified_at is None


@pytest.mark.statutory
def test_verification_brings_a_version_into_force(db, fixture_file, loader_user, verifier_user):
    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader_user.email)
    call_command(
        "verifystatutory",
        "REF-TEST-CMD",
        "--verified-by",
        verifier_user.email,
        "--current-through",
        "2027-02-28",
        "--golden-tests-passed",
    )

    version = ReferenceDataVersion.objects.get(version_label="REF-TEST-CMD")
    assert version.verified_by_user == verifier_user
    assert version.data_current_through == datetime.date(2027, 2, 28)
    assert version.is_usable is True
    assert ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1)) == version


@pytest.mark.statutory
def test_a_version_whose_golden_tests_fail_never_comes_into_force(
    db, fixture_file, loader_user, verifier_user
):
    """Verified by a person, and still refused by the system until the published
    worked examples reproduce."""
    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader_user.email)
    call_command(
        "verifystatutory",
        "REF-TEST-CMD",
        "--verified-by",
        verifier_user.email,
        "--current-through",
        "2027-02-28",
    )

    version = ReferenceDataVersion.objects.get()
    assert version.is_verified is True
    assert version.is_usable is False
    assert ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1)) is None


@pytest.mark.statutory
def test_verification_cannot_claim_data_is_current_before_it_applies(
    db, fixture_file, loader_user, verifier_user
):
    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader_user.email)

    with pytest.raises(CommandError):
        call_command(
            "verifystatutory",
            "REF-TEST-CMD",
            "--verified-by",
            verifier_user.email,
            "--current-through",
            "2025-12-31",
        )


@pytest.mark.statutory
def test_the_staleness_guard_reads_what_verification_wrote(
    db, fixture_file, loader_user, verifier_user
):
    """The whole chain, end to end: load, verify, and a run past the confirmed date
    is blocked rather than computed."""
    from statutory.staleness import ReferenceDataStaleError, assert_reference_data_covers

    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader_user.email)
    call_command(
        "verifystatutory",
        "REF-TEST-CMD",
        "--verified-by",
        verifier_user.email,
        "--current-through",
        "2027-02-28",
        "--golden-tests-passed",
    )

    assert assert_reference_data_covers(datetime.date(2027, 2, 28))

    with pytest.raises(ReferenceDataStaleError):
        assert_reference_data_covers(datetime.date(2027, 3, 31))
