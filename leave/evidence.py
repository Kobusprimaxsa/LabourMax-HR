"""The sick-leave evidence catalogue — P6 chunk 2, task 1.

Four rows, all pointing at the shared ``SICK`` leave type. Read the model
docstring on ``LeaveEvidenceType`` first: **evidence gates pay, not leave.**
BCEA s23 conditions the employer's right to WITHHOLD PAY on the absence
exceeding a threshold with no certificate produced — it never conditions the
right to TAKE the leave itself. Nothing that reads this catalogue may use it
to refuse an application; only to decide ``is_paid``.

``max_consecutive_days_without_note`` is BCEA s23(1)'s own day count, read
through ``statutory.resolve.parameter_value()`` from
``SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS`` — never invented here, and this
seed RAISES if it is not loaded rather than guessing zero or skipping the
row (the same "resolve raises when missing" rule P6 chunk 1 already applies
to accrual figures).

FLAGGED, because the boundary between two of these four codes is a genuine
interpretive choice rather than something sheet 02 or the Act spells out:

- ``NO_NOTE.is_paid_by_default`` — set FALSE, the reading that this row
  represents an absence that has already gone past what BCEA s23(1) excuses
  with no proof. Whether a given application actually lands on ``NO_NOTE``
  or ``SELF_CERTIFIED`` is not this catalogue's decision — it depends on
  how many consecutive days that SPECIFIC application covers against the
  SAME threshold, which is the per-day computation Task 2 builds, not a
  property fixed once here. Flagging the DEFAULT rather than presenting it
  as a settled per-application outcome.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from leave.models import LeaveEvidenceType, LeaveType
from statutory import resolve

Code = LeaveEvidenceType.Code

#: The statutory parameter this seed reads BCEA s23(1)'s day count from.
#: See tools/build_leave_fixture.py and reference/ref-2026.03.01-leave.json.
SICK_CERTIFICATE_THRESHOLD_PARAMETER = "SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS"


@dataclass(frozen=True)
class SystemEvidenceType:
    """One catalogue entry, and why it is shaped the way it is."""

    code: str
    name: str
    reason: str
    requires_attachment: bool = False
    is_paid_by_default: bool = True
    uses_threshold: bool = False
    display_order: int = 0
    flagged: str = ""


SYSTEM_EVIDENCE_TYPES: tuple[SystemEvidenceType, ...] = (
    SystemEvidenceType(
        code=Code.DOCTOR_NOTE,
        name="Doctor's note",
        requires_attachment=True,
        is_paid_by_default=True,
        display_order=10,
        reason=(
            "Independent medical evidence — BCEA s23(1)'s own condition for pay is "
            "satisfied outright, so there is no consecutive-day threshold to apply "
            "(max_consecutive_days_without_note stays NULL: the note IS the proof "
            "the threshold exists to demand)."
        ),
    ),
    SystemEvidenceType(
        code=Code.CLINIC_NOTE,
        name="Clinic note",
        requires_attachment=True,
        is_paid_by_default=True,
        display_order=20,
        reason=(
            "A clinic sister's note is ordinary, accepted medical evidence under "
            "s23(1) in the same way a doctor's note is — same shape as DOCTOR_NOTE."
        ),
    ),
    SystemEvidenceType(
        code=Code.SELF_CERTIFIED,
        name="Self-certified",
        requires_attachment=False,
        is_paid_by_default=True,
        uses_threshold=True,
        display_order=30,
        reason=(
            "BCEA s23(1) does not require a certificate at all within its own "
            "threshold — an absence of the permitted length or fewer occasions is "
            "paid on the employee's own word. is_paid_by_default=True inside that "
            "threshold; Task 2's per-day computation is what actually tests a given "
            "application's length against it."
        ),
    ),
    SystemEvidenceType(
        code=Code.NO_NOTE,
        name="No note produced",
        requires_attachment=False,
        is_paid_by_default=False,
        uses_threshold=True,
        display_order=40,
        flagged=(
            "is_paid_by_default reads as 'beyond the threshold, unpaid' — the exact "
            "boundary against SELF_CERTIFIED is a per-application computation "
            "(Task 2), not fixed by this catalogue row alone."
        ),
        reason=(
            "BCEA s23(1)'s own permission to withhold pay applies exactly here: no "
            "certificate produced, and (per the per-application check) the absence "
            "exceeds what the threshold excuses without one."
        ),
    ),
)

FLAGGED_CODES: tuple[str, ...] = tuple(spec.code for spec in SYSTEM_EVIDENCE_TYPES if spec.flagged)


class LeaveEvidenceSeedError(Exception):
    """The seed cannot proceed. Nothing was written."""


def seed_system_evidence_types() -> list[LeaveEvidenceType]:
    """Create any system evidence type that does not exist yet, against the
    shared ``SICK`` leave type. Returns what was created.

    Idempotent, and deliberately never updates. Raises if ``SICK`` has not
    been seeded yet (``leave.types.seed_system_leave_types()`` must run
    first) or if BCEA s23(1)'s day count is not loaded — resolving that
    figure is not optional, the same "resolve raises when missing" rule
    chunk 1's accrual engine already applies.
    """
    try:
        sick = LeaveType.objects.get(code=LeaveType.Code.SICK, tenant__isnull=True)
    except LeaveType.DoesNotExist as error:
        raise LeaveEvidenceSeedError(
            "The SICK leave type has not been seeded yet. Run "
            "`manage.py seedleavetypes` first — leave_evidence_type rows point at it."
        ) from error

    threshold = None
    if any(spec.uses_threshold for spec in SYSTEM_EVIDENCE_TYPES):
        try:
            threshold = int(
                resolve.parameter_value(SICK_CERTIFICATE_THRESHOLD_PARAMETER, timezone.localdate())
            )
        except resolve.StatutoryValueMissingError as error:
            raise LeaveEvidenceSeedError(
                f"{SICK_CERTIFICATE_THRESHOLD_PARAMETER} is not loaded as at "
                f"{timezone.localdate():%d %B %Y}. Run `manage.py loadstatutory --all` "
                f"before seeding evidence types — BCEA s23(1)'s day count decides "
                f"whether pay may lawfully be withheld, and this codebase does not "
                f"guess it."
            ) from error

    created: list[LeaveEvidenceType] = []
    with transaction.atomic():
        existing_codes = set(
            LeaveEvidenceType.objects.filter(leave_type=sick).values_list("code", flat=True)
        )

        for spec in SYSTEM_EVIDENCE_TYPES:
            if spec.code in existing_codes:
                continue

            evidence_type = LeaveEvidenceType(
                leave_type=sick,
                code=spec.code,
                name=spec.name,
                requires_attachment=spec.requires_attachment,
                is_paid_by_default=spec.is_paid_by_default,
                max_consecutive_days_without_note=threshold if spec.uses_threshold else None,
                display_order=spec.display_order,
            )
            evidence_type.full_clean()
            evidence_type.save()
            created.append(evidence_type)

    return created
