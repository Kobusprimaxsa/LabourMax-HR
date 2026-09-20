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
from django.core.management.base import CommandError

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


# ------------- a superseded machine tick is history, not a standing warning


def supersede(label, *, new_label):
    """Supersede a loaded version the way the loader does: a NEW version row
    pointing at the old one. Never a deletion (D-199)."""
    old = ReferenceDataVersion.objects.get(version_label=label)
    return ReferenceDataVersion.objects.create(
        version_label=new_label,
        applies_from=old.applies_from,
        description="Re-encoded.",
        supersedes=old,
        supersede_reason="Normalised the citation.",
    )


@pytest.mark.statutory
def test_a_superseded_machine_tick_is_not_a_standing_warning(loaded, machine_user):
    """FOUR OF THESE WARNED ON EVERY RUN, FOREVER. The message said so itself —
    "(superseded, so nothing reads it)" — and a command that never returns
    clean trains people to skim it, which is how a real finding gets missed.

    Nothing reads a superseded version: ``in_force_on()`` cannot see it and no
    run can reach it. It is kept only as the audit record (D-199), so its
    development verifier is a fact about history rather than something anybody
    can act on.
    """
    verify(MACHINE)
    supersede("REF-TEST-MACHINE", new_label="REF-TEST-MACHINE-r2")

    assert checks.check_machine_verified_versions() == []
    assert checks.superseded_machine_verified() == ["REF-TEST-MACHINE"], (
        "dropped from the warnings, but still countable - a check that went "
        "silent could not be told apart from one that stopped working"
    )


@pytest.mark.statutory
def test_a_live_machine_tick_still_warns_once_superseded_ones_are_split_out(loaded, machine_user):
    """Watched NOT firing, in the direction that matters. If the split were
    keyed wrongly this would go quiet and the live finding would vanish with
    the historical ones."""
    verify(MACHINE)

    assert len(checks.check_machine_verified_versions()) == 1
    assert checks.superseded_machine_verified() == []


@pytest.mark.statutory
def test_checkstatutory_reports_superseded_ticks_as_a_note_not_a_warning(
    loaded, machine_user, capsys
):
    """The acceptance test: with nothing but superseded machine ticks, the
    command reconciles. One factual line, not four warnings in with the live
    findings."""
    verify(MACHINE)
    supersede("REF-TEST-MACHINE", new_label="REF-TEST-MACHINE-r2")

    # This fixture holds one parameter and no wage rates or tax years, so
    # check_something_is_loaded() blocks for its own unrelated reason (D-74).
    # What matters here is the WARNING count: it must be nil.
    with pytest.raises(CommandError):
        call_command("checkstatutory")

    output = capsys.readouterr().out
    assert "0 to look at" in output, (
        "four of these warned on every run forever; a superseded machine tick "
        "must not stand in the same list as a live finding"
    )
    assert "[warning]" not in output, "history is not a warning"
    assert "1 superseded version" in output
    assert "nothing reads them" in output


# ---------------- a machine among the OTHER checkers, not just the signer


@pytest.mark.statutory
def test_a_machine_as_the_second_checker_is_reported(loaded, machine_user):
    """O-37. D-270 let a version carry several checkers and widened both halves
    of the GATE to match — ``is_machine_verified`` and ``unusable_q()`` treat a
    version as machine-verified if ANY checker is a development identity. This
    check was not widened with them, so a version whose machine identity was
    the SECOND name on it was refused by the payroll gate and never mentioned
    by ``checkstatutory``: somebody would watch a run refuse with no report
    saying which version to re-verify.
    """
    person = AppUser.objects.create_user(email="kobus@example.com", password="x" * 16)
    call_command(
        "verifystatutory",
        "REF-TEST-MACHINE",
        "--verified-by",
        person.email,
        "--also-checked-by",
        MACHINE,
        "--current-through",
        "2027-02-28",
        "--golden-tests-passed",
    )

    issues = checks.check_machine_verified_versions()

    assert len(issues) == 1, "the gate already refuses this version; the report must say so"
    assert MACHINE in issues[0].message, "name the MACHINE, not whoever happened to sign"
    assert person.email not in issues[0].message, (
        "the human checker did nothing wrong and must not be named as the problem"
    )


@pytest.mark.statutory
def test_the_report_and_the_gate_agree_about_every_version(loaded, machine_user):
    """The property that makes O-37 impossible to reopen: whatever the gate
    treats as machine-verified, the report names — one expression, not two
    lists that have to be kept in step."""
    person = AppUser.objects.create_user(email="kobus@example.com", password="x" * 16)
    call_command(
        "verifystatutory",
        "REF-TEST-MACHINE",
        "--verified-by",
        person.email,
        "--also-checked-by",
        MACHINE,
        "--current-through",
        "2027-02-28",
        "--golden-tests-passed",
    )

    reported = {
        issue.where.removeprefix("reference_data_version ")
        for issue in checks.check_machine_verified_versions()
    } | set(checks.superseded_machine_verified())
    by_the_gate = {
        version.version_label
        for version in ReferenceDataVersion.objects.all()
        if version.is_machine_verified
    }

    assert reported == by_the_gate
