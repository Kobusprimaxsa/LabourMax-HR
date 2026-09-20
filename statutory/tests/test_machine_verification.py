"""A verification tick that is not a person's (D-262).

The gate half of this lives in ``payroll/tests/test_validation.py``, where a run
with fourteen human-verified versions and four machine-verified ones must still
refuse. What is proven here is the other three points the rule touches: the
command ACCEPTS and says so loudly, ``checkstatutory`` REPORTS without refusing,
and ``is_usable`` is False.

**Why accept at all.** Every calculator, the leave engine and the payroll run
read rows that ``in_force_on()`` hides until something has ticked them, so a
development database that could not record a tick could not be built on. The
answer is not to refuse the tick; it is to make the tick worthless where it
matters and impossible to miss everywhere else.
"""

from __future__ import annotations

import datetime
import json

import pytest
from django.core.management import call_command

from core.models import AppUser
from statutory import checks
from statutory.models import ReferenceDataVersion

MACHINE = "claude-verification@labourmax.invalid"


@pytest.fixture
def fixture_file(tmp_path):
    document = {
        "version_label": "REF-TEST-MACHINE",
        "applies_from": "2026-03-01",
        "tables": {
            "statutory_parameter": [
                {
                    "parameter_code": "TEST_PARAMETER",
                    "value_numeric": "1.000000",
                    "unit": "percent",
                    "effective_from": "2026-03-01",
                    "source_reference": "Test fixture, not a real gazette",
                }
            ]
        },
    }
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def loaded(db, fixture_file):
    loader = AppUser.objects.create_user(email="loader@example.com", password="x" * 16)
    call_command("loadstatutory", str(fixture_file), "--loaded-by", loader.email)
    return ReferenceDataVersion.objects.get(version_label="REF-TEST-MACHINE")


@pytest.fixture
def machine_user(db):
    return AppUser.objects.create_user(email=MACHINE, password="x" * 16)


def verify(email):
    call_command(
        "verifystatutory",
        "REF-TEST-MACHINE",
        "--verified-by",
        email,
        "--current-through",
        "2027-02-28",
        "--golden-tests-passed",
    )


@pytest.mark.statutory
def test_verifystatutory_accepts_a_development_identity_and_warns(loaded, machine_user, capsys):
    """Accepting is deliberate. Saying nothing about it would not be."""
    verify(MACHINE)

    loaded.refresh_from_db()
    assert loaded.is_verified is True, "the tick is recorded - the build needs it"
    output = capsys.readouterr().out
    assert "DEVELOPMENT IDENTITY" in output
    assert "A person still has to do this." in output


@pytest.mark.statutory
def test_a_machine_tick_does_not_make_a_version_usable(loaded, machine_user):
    verify(MACHINE)

    loaded.refresh_from_db()
    assert loaded.is_machine_verified is True
    assert loaded.is_usable is False
    assert ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1)) is None


@pytest.mark.statutory
def test_checkstatutory_reports_it_and_does_not_refuse(loaded, machine_user):
    """A REFUSAL here would be self-defeating: run_all() gates verifystatutory
    itself, so a blocking issue would make the first machine verification
    impossible to record and the second impossible to undo."""
    verify(MACHINE)

    issues = checks.check_machine_verified_versions()

    assert len(issues) == 1
    assert issues[0].blocking is False
    assert "REF-TEST-MACHINE" in issues[0].where
    assert MACHINE in issues[0].message
    assert issues[0] in checks.run_all(), "not wired into run_all is not wired in at all"


@pytest.mark.statutory
def test_a_person_verifying_produces_no_issue_and_a_usable_version(loaded):
    """Watched NOT firing. Otherwise the test above proves only that a list is
    non-empty for some other reason."""
    person = AppUser.objects.create_user(email="kobus@example.com", password="x" * 16)
    verify(person.email)

    loaded.refresh_from_db()
    assert loaded.is_machine_verified is False
    assert loaded.is_usable is True
    assert checks.check_machine_verified_versions() == []
    assert ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1)) == loaded


@pytest.mark.statutory
@pytest.mark.parametrize(
    "email",
    [
        "someone@labourmax.invalid",
        "SOMEONE@LABOURMAX.INVALID",
        "a.b@sub.domain.invalid",
    ],
)
def test_the_suffix_is_matched_case_insensitively_and_on_any_domain(loaded, db, email):
    """The rule is RFC 2606's reserved TLD, not one hard-coded address. A list
    of known development accounts has to be maintained by whoever adds the next
    one, and the person who forgets is the person the guard exists for."""
    AppUser.objects.create_user(email=email, password="x" * 16)
    verify(email)

    loaded.refresh_from_db()
    assert loaded.is_machine_verified is True
    assert loaded.is_usable is False


@pytest.mark.statutory
def test_a_lookalike_domain_is_not_caught(loaded, db):
    """.invalid.com is a real, registrable domain and a real address could live
    there. The suffix must anchor at the end."""
    AppUser.objects.create_user(email="someone@labourmax.invalid.com", password="x" * 16)
    verify("someone@labourmax.invalid.com")

    loaded.refresh_from_db()
    assert loaded.is_machine_verified is False
    assert loaded.is_usable is True
