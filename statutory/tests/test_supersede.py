"""Re-encoding a fixture is a new VERSION of the file, not a new gazette (O-21).

Regenerating a fixture changes its SHA-256. ``check_fixture_checksums()`` then
refuses the file by name, and until now recovery meant deleting
``reference_data_version`` rows by hand and reloading — hand-deleting reference
data, which is what invariant 2 exists to forbid. A guard people route around by
hand has stopped being a guard.

The supported path: give the re-encoded file a new version label and load it with
``--supersede <old label> --reason "..."``. Nothing is deleted. The old version row
stays and is marked superseded, so it is still readable and a 2029 re-run still
answers "what was in force in March 2026" — a different question from "which
version of the file is current".

What it must refuse, because a re-encoding is only ever prose:

- a changed FIGURE — that is a new gazette and gets an ordinary load, with its
  own effective date. Named field by field, old against new
- a row the file adds or the database is missing — that is new data, not a
  re-encoding
- superseding a VERIFIED version, unless told explicitly, because it silently
  un-verifies the foundation a payroll run was computed against
- no reason
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError

from core.models import AppUser
from statutory import checks
from statutory.loader import SupersedeRefusedError, load_reference_data
from statutory.models import (
    ReferenceDataVersion,
    TerminationNoticeBand,
)

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

V1 = "REF-TEST-SUP"
V2 = "REF-TEST-SUP-r2"

DOCUMENT = {
    "version_label": V1,
    "applies_from": "2026-03-01",
    "description": "Test fixture",
    "tables": {
        "statutory_parameter": [
            {
                "parameter_code": "TEST_THRESHOLD",
                "value_numeric": "15.000000",
                "unit": "years",
                "effective_from": "2026-03-01",
                "source_reference": "Act 75 of 1997, s43(1)",
                "notes": "The original wording.",
            }
        ]
    },
}


@pytest.fixture
def loader(db):
    return AppUser.objects.create_user(email="loader@example.com", password="x" * 16)


@pytest.fixture
def verifier(db):
    return AppUser.objects.create_user(email="verifier@example.com", password="x" * 16)


def write(tmp_path, document, name="fixture.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def reencoded(**changes):
    """The same statutory facts, said differently — a new label and new prose."""
    document = copy.deepcopy(DOCUMENT)
    document["version_label"] = V2
    row = document["tables"]["statutory_parameter"][0]
    row["notes"] = "The corrected wording, citing the gazette page."
    row["source_reference"] = "Basic Conditions of Employment Act 75 of 1997, s43(1)"
    row.update(changes)
    return document


def load_v1(tmp_path, loader):
    call_command("loadstatutory", write(tmp_path, DOCUMENT), "--loaded-by", loader.email)


def supersede(tmp_path, loader, document, *, reason="Re-encoded citation", extra=()):
    args = [
        "loadstatutory",
        write(tmp_path, document, "reencoded.json"),
        "--loaded-by",
        loader.email,
        "--supersede",
        V1,
    ]
    if reason is not None:
        args += ["--reason", reason]
    return call_command(*args, *extra)


# ----------------------------------------------------------------- the refusals


def test_supersede_without_a_reason_is_refused(tmp_path, loader):
    load_v1(tmp_path, loader)
    with pytest.raises(CommandError) as raised:
        supersede(tmp_path, loader, reencoded(), reason=None)
    assert "--reason" in str(raised.value)
    assert ReferenceDataVersion.objects.count() == 1


def test_supersede_of_an_unknown_version_is_refused(tmp_path, loader):
    load_v1(tmp_path, loader)
    with pytest.raises(CommandError) as raised:
        call_command(
            "loadstatutory",
            write(tmp_path, reencoded(), "reencoded.json"),
            "--loaded-by",
            loader.email,
            "--supersede",
            "REF-DOES-NOT-EXIST",
            "--reason",
            "typo",
        )
    assert "REF-DOES-NOT-EXIST" in str(raised.value)


def test_a_changed_figure_is_refused_and_named(tmp_path, loader):
    """THE GUARD THAT MATTERS. A changed figure is a new gazette, not a
    re-encoding, and it gets an ordinary load with its own effective date."""
    load_v1(tmp_path, loader)

    with pytest.raises(CommandError) as raised:
        supersede(tmp_path, loader, reencoded(value_numeric="16.000000"))

    message = str(raised.value)
    assert "statutory_parameter" in message, message
    assert "value_numeric" in message, message
    assert "15.000000" in message and "16.000000" in message, message
    assert ReferenceDataVersion.objects.count() == 1, "nothing was written"
    assert ReferenceDataVersion.objects.get().version_label == V1


def test_a_row_the_database_does_not_have_is_refused(tmp_path, loader):
    """Adding a row is new data. A re-encoding says the same facts differently."""
    load_v1(tmp_path, loader)
    document = reencoded()
    document["tables"]["statutory_parameter"].append(
        {
            "parameter_code": "TEST_SECOND",
            "value_numeric": "1.000000",
            "unit": "years",
            "effective_from": "2026-03-01",
            "source_reference": "Act 75 of 1997, s43(1)",
        }
    )
    with pytest.raises(CommandError) as raised:
        supersede(tmp_path, loader, document)
    assert "TEST_SECOND" in str(raised.value)
    assert ReferenceDataVersion.objects.count() == 1


def test_superseding_a_verified_version_is_refused_unless_told(tmp_path, loader, verifier):
    load_v1(tmp_path, loader)
    call_command(
        "verifystatutory",
        V1,
        "--verified-by",
        verifier.email,
        "--current-through",
        "2027-02-28",
        "--golden-tests-passed",
    )

    with pytest.raises(CommandError) as raised:
        supersede(tmp_path, loader, reencoded())
    message = str(raised.value)
    assert "verified" in message, message
    assert ReferenceDataVersion.objects.count() == 1

    supersede(tmp_path, loader, reencoded(), extra=["--supersede-verified"])
    assert ReferenceDataVersion.objects.count() == 2


# ------------------------------------------------------------- the happy path


def test_a_re_encoding_supersedes_without_deleting_anything(tmp_path, loader):
    load_v1(tmp_path, loader)
    before = ReferenceDataVersion.objects.get(version_label=V1)

    supersede(tmp_path, loader, reencoded(), reason="D-198: the cap became a pair")

    old = ReferenceDataVersion.objects.get(version_label=V1)
    new = ReferenceDataVersion.objects.get(version_label=V2)
    assert old.pk == before.pk, "the old version row is kept, never deleted"
    assert old.superseded_by.pk == new.pk
    assert new.supersedes_id == old.pk
    assert "cap became a pair" in new.supersede_reason
    assert new.checksum and new.checksum != old.checksum

    from statutory.models import StatutoryParameter

    row = StatutoryParameter.objects.get(parameter_code="TEST_THRESHOLD")
    assert row.notes == "The corrected wording, citing the gazette page."
    assert row.source_reference.startswith("Basic Conditions of Employment Act")
    assert str(row.value_numeric) == "15.000000", "the figure is untouched"


def test_a_superseded_version_is_no_longer_in_force_but_is_still_readable(
    tmp_path, loader, verifier
):
    """'In force on a date' and 'the current version of the file' are different
    questions. A 2029 re-run of March 2026 still needs the first one answered."""
    import datetime

    load_v1(tmp_path, loader)
    supersede(tmp_path, loader, reencoded())
    for label in (V1, V2):
        version = ReferenceDataVersion.objects.get(version_label=label)
        version.verified_by_user = verifier
        version.verified_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
        version.golden_tests_passed = True
        version.save()

    in_force = ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1))
    assert in_force.version_label == V2, "the superseded version is not the current one"
    assert ReferenceDataVersion.objects.filter(version_label=V1).exists(), "still readable"


def test_the_checksum_check_reconciles_against_the_current_version(tmp_path, loader):
    """The drift this whole path exists to make recoverable: after superseding,
    the fixture directory holds only the new label, and the check is quiet."""
    load_v1(tmp_path, loader)
    document = reencoded()
    supersede(tmp_path, loader, document)

    # The re-encoded file is what now sits in the fixture directory.
    (tmp_path / "fixture.json").unlink()
    issues = checks.check_fixture_checksums(directory=tmp_path)
    assert not [i for i in issues if i.blocking], [i.message for i in issues]


def test_trailing_zeros_are_not_a_figure_change(tmp_path, loader):
    """0 and 0.00 are the same figure. Comparing the two as text reported a
    change that was not one, and refused a citation-only re-encoding."""
    load_v1(tmp_path, loader)
    supersede(tmp_path, loader, reencoded(value_numeric="15"), reason="same figure, fewer zeros")
    assert ReferenceDataVersion.objects.filter(version_label=V2).exists()


# ------------------------------- resolving a CONTESTED band (D-277)


CONTESTED_BAND_V1 = "REF-TEST-CONTESTED"
CONTESTED_BAND_V2 = "REF-TEST-CONTESTED-r2"


def band_document(label, *, bands):
    return {
        "version_label": label,
        "applies_from": "2026-03-01",
        "description": "Test notice bands",
        "tables": {
            "termination_rule_set": [
                {
                    "effective_from": "2026-03-01",
                    "source_reference": "Test agreement, clause 1",
                    "severance_weeks_per_completed_year": "1.00",
                    "severance_requires_operational_reason": True,
                    "annual_bonus_weeks": "0.000",
                    "annual_bonus_month": None,
                    "annual_bonus_pro_rata_on_termination": False,
                    "annual_bonus_min_service_months": 0,
                }
            ],
            "termination_notice_band": bands,
        },
    }


def band(sequence, *, to_value, to_unit, from_value="0", from_unit="weeks", **extra):
    row = {
        "sector": None,
        "sector_area": None,
        "effective_from": "2026-03-01",
        "sequence": sequence,
        "source_reference": "Test agreement, clause 2",
        "service_from_value": from_value,
        "service_from_unit": from_unit,
        "service_from_inclusive": sequence == 1,
        "service_to_value": to_value,
        "service_to_unit": to_unit,
        "service_to_inclusive": None if to_value is None else True,
        "notice_value": None,
        "notice_unit": "",
        # Written on every row, exactly as the real builder writes them: a
        # resolution that left is_contested True while setting a value would be
        # refused by notice_band_contested_has_no_value_and_a_reason, which is
        # the CHECK pair doing its job — see the test at the end of this file.
        "is_contested": False,
        "contested_reason": "",
    }
    row.update(extra)
    return row


@pytest.fixture
def contested(db):
    """A loaded rule set whose middle band says "this cannot be read"."""
    load_reference_data(
        band_document(
            CONTESTED_BAND_V1,
            bands=[
                band(1, to_value="4", to_unit="weeks", notice_value="1", notice_unit="days"),
                band(
                    2,
                    from_value="4",
                    from_unit="weeks",
                    to_value="6",
                    to_unit="months",
                    is_contested=True,
                    contested_reason="Two limbs, two answers.",
                ),
                band(
                    3,
                    from_value="6",
                    from_unit="months",
                    to_value=None,
                    to_unit="",
                    notice_value="2",
                    notice_unit="weeks",
                ),
            ],
        )
    )


def resolution_document(label):
    """The same file with the contested band read again, plus its twin."""
    return band_document(
        label,
        bands=[
            band(1, to_value="4", to_unit="weeks", notice_value="1", notice_unit="days"),
            band(
                2,
                from_value="4",
                from_unit="weeks",
                to_value="6",
                to_unit="months",
                notice_value="1",
                notice_unit="weeks",
                probation_condition="on_probation",
            ),
            band(
                3,
                from_value="6",
                from_unit="months",
                to_value=None,
                to_unit="",
                notice_value="2",
                notice_unit="weeks",
            ),
            band(
                4,
                from_value="4",
                from_unit="weeks",
                to_value="6",
                to_unit="months",
                notice_value="2",
                notice_unit="weeks",
                probation_condition="off_probation",
            ),
        ],
    )


def test_a_contested_band_may_be_resolved_and_gain_its_twin(contested):
    """A contested band holds NO figure — notice_value is NULL and
    is_contested says why — so filling it in edits nothing. That is the whole
    of what "a statutory value is never edited" protects, and it is why a
    corrected READING had nowhere to land before D-277: supersede refused it as
    a figure change and an ordinary load refused it as an edit."""
    load_reference_data(
        resolution_document(CONTESTED_BAND_V2),
        supersede=CONTESTED_BAND_V1,
        reason="The two limbs are a general rule and a probation exception.",
    )

    bands = TerminationNoticeBand.objects.order_by("sequence")
    assert [b.probation_condition for b in bands] == [
        "any",
        "on_probation",
        "any",
        "off_probation",
    ]
    assert bands[1].is_contested is False
    assert bands[1].notice_value == Decimal("1.00")
    assert not checks.check_notice_bands(), "and both lanes still tile"


def test_a_band_that_was_never_contested_may_not_change_its_figure(contested):
    """The allowance is narrow on purpose. Band 3 holds a real two weeks, and
    moving it is a new gazette however it is dressed up."""
    document = resolution_document(CONTESTED_BAND_V2)
    document["tables"]["termination_notice_band"][2]["notice_value"] = "4"

    with pytest.raises(SupersedeRefusedError) as raised:
        load_reference_data(
            document, supersede=CONTESTED_BAND_V1, reason="Sneaking a figure through."
        )

    assert "a FIGURE changed" in str(raised.value)
    assert "notice_value" in str(raised.value)


def test_an_unconditional_new_row_is_still_refused(contested):
    """Adding a row is new data. Only the other LANE of a band being resolved
    comes in this way, and an unconditional row is never that."""
    document = resolution_document(CONTESTED_BAND_V2)
    document["tables"]["termination_notice_band"][3].pop("probation_condition")

    with pytest.raises(SupersedeRefusedError) as raised:
        load_reference_data(document, supersede=CONTESTED_BAND_V1, reason="New band.")

    assert "new data rather than a re-encoding" in str(raised.value)


def test_a_twin_with_nothing_to_be_the_twin_of_is_refused(contested):
    """A conditional band on its own is meaningless: it says one week applies
    to some employees and leaves the rest with no band at all. It arrives with
    the resolution it belongs to, or not at all."""
    document = resolution_document(CONTESTED_BAND_V2)
    # Put the contested band back exactly as loaded, so nothing is resolved.
    document["tables"]["termination_notice_band"][1] = band(
        2,
        from_value="4",
        from_unit="weeks",
        to_value="6",
        to_unit="months",
        is_contested=True,
        contested_reason="Two limbs, two answers.",
    )

    with pytest.raises(SupersedeRefusedError) as raised:
        load_reference_data(document, supersede=CONTESTED_BAND_V1, reason="Twin alone.")

    assert "resolves no contested band" in str(raised.value)


def test_a_resolution_that_forgets_to_clear_is_contested_is_refused(contested):
    """The CHECK pair (D-241), watched firing on the new path. Setting a value
    while leaving is_contested True would be a band that says both "here is the
    period" and "this cannot be read", and the database refuses it by name
    rather than letting the loader decide."""
    document = resolution_document(CONTESTED_BAND_V2)
    document["tables"]["termination_notice_band"][1]["is_contested"] = True
    document["tables"]["termination_notice_band"][1]["contested_reason"] = "Left behind."

    with pytest.raises(IntegrityError) as raised:
        load_reference_data(document, supersede=CONTESTED_BAND_V1, reason="Half a resolution.")

    assert "notice_band_contested_has_no_value_and_a_reason" in str(raised.value)
