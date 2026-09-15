"""``leave_evidence_type`` — evidence gates pay, not leave (task 1, task 6).

No tenant field: this is pure reference data, read without any context
pinned, and guarded against DELETE by trigger because a tenant-scoped table
(``leave_application``, under FORCE ROW LEVEL SECURITY) will point at it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import DatabaseError, transaction

from leave.evidence import (
    SICK_CERTIFICATE_THRESHOLD_PARAMETER,
    SYSTEM_EVIDENCE_TYPES,
    LeaveEvidenceSeedError,
    seed_system_evidence_types,
)
from leave.models import LeaveEvidenceType
from leave.types import seed_system_leave_types
from statutory.models import StatutoryParameter

pytestmark = pytest.mark.django_db


@pytest.fixture
def sick_certificate_threshold(db):
    return StatutoryParameter.objects.create(
        parameter_code=SICK_CERTIFICATE_THRESHOLD_PARAMETER,
        value_numeric=Decimal("2.000000"),
        unit=StatutoryParameter.Unit.DAYS,
        effective_from=datetime.date(1997, 12, 1),
        source_reference="Basic Conditions of Employment Act 75 of 1997, s23(1)",
    )


@pytest.fixture
def sick_type(db):
    seed_system_leave_types()


def test_seeding_refuses_without_sick_leave_type_seeded(db, sick_certificate_threshold):
    with pytest.raises(LeaveEvidenceSeedError) as raised:
        seed_system_evidence_types()

    assert "SICK leave type has not been seeded" in str(raised.value)


def test_seeding_refuses_without_the_threshold_loaded(sick_type):
    with pytest.raises(LeaveEvidenceSeedError) as raised:
        seed_system_evidence_types()

    assert SICK_CERTIFICATE_THRESHOLD_PARAMETER in str(raised.value)


def test_seeding_creates_all_four_variants(sick_type, sick_certificate_threshold):
    created = seed_system_evidence_types()

    assert {c.code for c in created} == {spec.code for spec in SYSTEM_EVIDENCE_TYPES}
    assert LeaveEvidenceType.objects.count() == 4


def test_seeding_twice_creates_nothing(sick_type, sick_certificate_threshold):
    seed_system_evidence_types()
    second = seed_system_evidence_types()

    assert second == []
    assert LeaveEvidenceType.objects.count() == 4


def test_doctor_note_is_paid_by_default_with_no_threshold(sick_type, sick_certificate_threshold):
    seed_system_evidence_types()

    doctor_note = LeaveEvidenceType.objects.get(code=LeaveEvidenceType.Code.DOCTOR_NOTE)

    assert doctor_note.is_paid_by_default is True
    assert doctor_note.requires_attachment is True
    assert doctor_note.max_consecutive_days_without_note is None


def test_no_note_is_unpaid_by_default_and_carries_the_resolved_threshold(
    sick_type, sick_certificate_threshold
):
    seed_system_evidence_types()

    no_note = LeaveEvidenceType.objects.get(code=LeaveEvidenceType.Code.NO_NOTE)

    assert no_note.is_paid_by_default is False
    assert no_note.max_consecutive_days_without_note == 2


def test_the_threshold_is_never_a_literal_in_this_module():
    """A defensive re-statement of test_no_hardcoded_rates' own point: the
    figure genuinely comes from the resolved parameter, not a fallback."""
    import ast
    import pathlib

    source = pathlib.Path("leave/evidence.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    literal_twos = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == 2 and isinstance(node.value, int)
    ]
    assert literal_twos == [], "The day count must come from statutory.resolve, never a literal 2."


def test_leave_evidence_type_cannot_be_deleted_by_trigger(sick_type, sick_certificate_threshold):
    """D-76's guard, on this table: a reference table a tenant-scoped table
    (leave_application) points at must refuse DELETE regardless of who is
    asking, because FORCE RLS on the referencing table would otherwise hide
    the very rows that should have stopped the delete."""
    seed_system_evidence_types()
    doctor_note = LeaveEvidenceType.objects.get(code=LeaveEvidenceType.Code.DOCTOR_NOTE)

    with pytest.raises(DatabaseError) as raised, transaction.atomic():
        doctor_note.delete()

    assert "cannot be deleted" in str(raised.value).lower()
