"""The system leave type catalogue — D-127 shipped the table, this stocks it.

Ten codes, all shared (``tenant=None``, ``is_system=True``) — the platform's own
reading of the BCEA/SD7/SD1 leave regime, which every tenant reads and none may
edit (the same shape as ``payroll_component``, D-87, and for the same reason:
``lock_system_rows()`` keys on ``is_system``, and every shared row must carry it
or a tenant editing the catalogue is one ``UPDATE`` away, D-93/D-134).

**No entitlement FIGURE lives here.** How many days, and how they accrue, are
``leave_rule_set`` columns with a citation. This table carries the SHAPE of a
leave type — whether it has a balance at all, whether that balance is paid,
whether it survives termination — which is a much smaller, much more stable
set of facts than the numbers themselves.

Where the shape is a genuine BCEA rule, it is cited below. Four codes are not
BCEA leave at all — ``STUDY`` and ``COMPASSIONATE`` are common contractual
benefits with no statutory shape to read, and the exact evidentiary and
payment position for FAMILY_RESPONSIBILITY is conditioned by the Act's own
wording rather than settled by it. Those are FLAGGED in the table below and in
the module docstring's own summary, not silently decided.

FLAGGED, because no rule set field or cited provision settles them outright:

- ``FAMILY_RESPONSIBILITY.requires_evidence`` — s27(4) says the employer "may
  not pay ... unless the employer has REASONABLE PROOF", which is conditional
  on the employer asking, not a blanket requirement the way a sick certificate
  is. Loaded ``True`` as the common, defensible reading; an employer that never
  asks is not thereby non-compliant.
- ``ADOPTION.requires_evidence`` — the adoption order is the obvious evidence,
  but nothing in BCEA s25B mandates the employer see it before granting leave.
  Loaded ``True`` for the same reason as above.
- ``STUDY`` and ``COMPASSIONATE`` — not BCEA leave at all. Every shape flag on
  these two rows is a plausible default for a discretionary benefit, not a
  compliance reading, and ``accrues=False`` on both is deliberate: with no
  statutory figure to accrue FROM, inventing a progressive accrual here would
  be exactly the kind of number this codebase never writes without a citation.
  An employer wanting either of these to accrue configures it via
  ``employee_leave_entitlement``.
- ``MATERNITY``, ``PARENTAL`` and ``ADOPTION.accrues`` — all ``False``. None is
  a day-bank an employee draws down; each is a calendar-bound entitlement
  (BCEA ss 25, 25A, 25B) with a start and a length, not a balance a monthly
  accrual run adds to. Chunk 1's engine (``leave/accrual.py``) does not
  attempt to grant these, and neither does this seed — a later chunk that
  builds the application flow decides how the entitlement is recorded when
  the leave is actually taken.

Idempotent and never updates — a component (or here, a leave type) that a
finalised payslip line or an ``employee_leave_entitlement`` row already points
at must not change under either of them, and the system-row lock would refuse
the write in any case (D-93).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from core.managers import platform_context
from leave.models import LeaveType

Code = LeaveType.Code
BalanceSource = LeaveType.BalanceSource


@dataclass(frozen=True)
class SystemLeaveType:
    """One catalogue entry, and why it is shaped the way it is."""

    code: str
    name: str
    reason: str
    parent_code: str | None = None
    balance_source: str = BalanceSource.OWN
    is_paid: bool = True
    is_statutory: bool = True
    accrues: bool = True
    cycle_months: int = 12
    requires_evidence: bool = False
    payable_on_termination: bool = False
    reduces_pay_when_exhausted: bool = True
    counts_as_service: bool = True
    colour_hex: str = "#4A7C9E"
    display_order: int = 0
    flagged: str = ""


SYSTEM_LEAVE_TYPES: tuple[SystemLeaveType, ...] = (
    SystemLeaveType(
        code=Code.ANNUAL,
        name="Annual leave",
        display_order=10,
        cycle_months=12,
        payable_on_termination=True,
        colour_hex="#2E7D32",
        reason=(
            "BCEA s20: the core accrual. cycle_months=12 and payable_on_termination=True "
            "are s40(b) — unused annual leave MUST be paid out, unlike every other type "
            "here. The day count itself is leave_rule_set's, read per s20(2)'s three "
            "accrual methods through employee_leave_entitlement.accrual_method (D-A)."
        ),
    ),
    SystemLeaveType(
        code=Code.ANNUAL_UNAUTHORISED,
        name="Annual leave — unauthorised",
        parent_code=Code.ANNUAL,
        balance_source=BalanceSource.PARENT,
        is_paid=False,
        accrues=False,
        display_order=11,
        colour_hex="#C62828",
        reason=(
            "Unauthorised absence recorded against the annual balance rather than "
            "handed back to the employee (D-127's own reasoning for balance_source). "
            "accrues=False and is_paid=False by construction: it never earns anything "
            "of its own, it only spends the parent's."
        ),
    ),
    SystemLeaveType(
        code=Code.SICK,
        name="Sick leave",
        display_order=20,
        cycle_months=36,
        requires_evidence=True,
        colour_hex="#EF6C00",
        reason=(
            "BCEA s22: a 36-month cycle (not 12), and s23(2) is the ordinary reading "
            "behind requiring evidence — though the Act only compels a certificate "
            "after two consecutive days off or a second absence in eight weeks, not "
            "for one isolated day. payable_on_termination stays False: s22 leave "
            "lapses, it is never cashed out. Accrual is NOT the day-based monthly "
            "shape ANNUAL uses — s22(2)'s first-six-months-of-employment rule "
            "(one day per 26 days worked) is attendance-based, and thereafter the "
            "cycle's own entitlement is credited at the point of eligibility, not "
            "smeared over 36 months. See leave/accrual.py."
        ),
    ),
    SystemLeaveType(
        code=Code.FAMILY_RESPONSIBILITY,
        name="Family responsibility leave",
        display_order=30,
        cycle_months=12,
        requires_evidence=True,
        colour_hex="#6A1B9A",
        reason=(
            "BCEA s27: five days (SD7 departs to five for domestic workers "
            "regardless — D-131's leave_rule_set carries the figure), granted for "
            "the whole cycle rather than accrued month by month, and lapsing at "
            "cycle end (s27(5)) — never payable_on_termination. requires_evidence "
            "FLAGGED: s27(4) conditions non-payment on the employer actually asking "
            "for reasonable proof, which is not the same as always requiring it."
        ),
    ),
    SystemLeaveType(
        code=Code.MATERNITY,
        name="Maternity leave",
        is_paid=False,
        accrues=False,
        payable_on_termination=False,
        counts_as_service=True,
        display_order=40,
        colour_hex="#AD1457",
        reason=(
            "BCEA s25: unpaid by the employer — UIF's maternity benefit is a "
            "separate claim this system does not run payroll for. accrues=False "
            "because it is a calendar-bound period (weeks before/after birth, "
            "already modelled on leave_rule_set's maternity_* fields), not a day-bank "
            "a monthly run adds to."
        ),
    ),
    SystemLeaveType(
        code=Code.PARENTAL,
        name="Parental leave",
        is_paid=False,
        accrues=False,
        display_order=41,
        colour_hex="#8E24AA",
        reason=(
            "BCEA s25A (the interim Van Wyk reading-in, D-... in leave_rule_set): "
            "unpaid by the employer, same UIF-benefit shape as maternity, and "
            "calendar-bound rather than accrued."
        ),
    ),
    SystemLeaveType(
        code=Code.ADOPTION,
        name="Adoption leave",
        is_paid=False,
        accrues=False,
        requires_evidence=True,
        display_order=42,
        colour_hex="#5E35B1",
        reason=(
            "BCEA s25B: same unpaid, calendar-bound shape as parental leave. "
            "requires_evidence FLAGGED — the adoption order is the obvious proof "
            "but the Act does not itself mandate producing it before leave starts."
        ),
    ),
    SystemLeaveType(
        code=Code.UNPAID,
        name="Unpaid leave",
        is_paid=False,
        is_statutory=False,
        accrues=False,
        balance_source=BalanceSource.NONE,
        reduces_pay_when_exhausted=False,
        display_order=90,
        colour_hex="#757575",
        reason=(
            "Not a BCEA entitlement at all — a captured fact about a day nobody "
            "was paid for. balance_source=none: there is no balance to have, so "
            "there is nothing for a monthly run to touch and nothing to exhaust."
        ),
    ),
    SystemLeaveType(
        code=Code.STUDY,
        name="Study leave",
        is_statutory=False,
        accrues=False,
        payable_on_termination=False,
        display_order=91,
        colour_hex="#00838F",
        flagged="Not BCEA leave. Every shape flag is a discretionary default.",
        reason=(
            "No statutory basis at all — a contractual benefit some employers "
            "grant. FLAGGED: is_paid, requires_evidence and accrues are all "
            "plausible defaults, not compliance readings. An employer wanting "
            "this to accrue configures it per employee via "
            "employee_leave_entitlement, which this seed deliberately does not "
            "pre-empt with an invented rule set figure."
        ),
    ),
    SystemLeaveType(
        code=Code.COMPASSIONATE,
        name="Compassionate leave",
        is_statutory=False,
        accrues=False,
        payable_on_termination=False,
        display_order=92,
        colour_hex="#4E342E",
        flagged="Not BCEA leave. Every shape flag is a discretionary default.",
        reason=(
            "Same position as STUDY: a contractual benefit, not a statutory one. "
            "Employers commonly grant a handful of days per event rather than a "
            "per-cycle accrual, which is a policy fact employee_leave_entitlement "
            "carries per employee, not a catalogue-wide default this row can state."
        ),
    ),
)

#: Every entry above whose shape is a plausible default rather than a settled
#: reading — surfaced by ``--list`` and by the command's own summary, so the
#: gap is visible on every run rather than only in this file's prose.
FLAGGED_CODES: tuple[str, ...] = tuple(spec.code for spec in SYSTEM_LEAVE_TYPES if spec.flagged)


class LeaveTypeSeedError(Exception):
    """The seed cannot proceed. Nothing was written."""


def seed_system_leave_types() -> list[LeaveType]:
    """Create any system leave type that does not exist yet. Returns what was created.

    Idempotent, and deliberately never updates — an ``employee_leave_entitlement``
    row or a future leave transaction points at these by id, and the system-row
    lock would refuse a write to an existing one in any case (D-93).

    Parent rows must be created before their children in ``SYSTEM_LEAVE_TYPES``'
    own order — ``ANNUAL_UNAUTHORISED`` names ``ANNUAL`` by code, and the lookup
    below only ever searches what this same call has already created or found,
    never across a partially-seeded set from two concurrent calls.
    """
    created: list[LeaveType] = []
    with transaction.atomic(), platform_context():
        by_code: dict[str, LeaveType] = {
            row.code: row
            for row in LeaveType.objects.filter(tenant__isnull=True).select_related(
                "parent_leave_type"
            )
        }

        for spec in SYSTEM_LEAVE_TYPES:
            if spec.code in by_code:
                continue

            parent = None
            if spec.parent_code:
                parent = by_code.get(spec.parent_code)
                if parent is None:
                    raise LeaveTypeSeedError(
                        f"{spec.code} names {spec.parent_code} as its parent, but "
                        f"{spec.parent_code} has not been seeded. Fix SYSTEM_LEAVE_TYPES' "
                        f"order — a parent must appear before its children."
                    )

            leave_type = LeaveType(
                tenant=None,
                code=spec.code,
                name=spec.name,
                parent_leave_type=parent,
                balance_source=spec.balance_source,
                is_paid=spec.is_paid,
                is_statutory=spec.is_statutory,
                accrues=spec.accrues,
                cycle_months=spec.cycle_months,
                requires_evidence=spec.requires_evidence,
                payable_on_termination=spec.payable_on_termination,
                reduces_pay_when_exhausted=spec.reduces_pay_when_exhausted,
                counts_as_service=spec.counts_as_service,
                is_system=True,
                colour_hex=spec.colour_hex,
                display_order=spec.display_order,
                is_active=True,
            )
            leave_type.full_clean(exclude=["tenant"])
            leave_type.save()
            by_code[spec.code] = leave_type
            created.append(leave_type)

    return created
