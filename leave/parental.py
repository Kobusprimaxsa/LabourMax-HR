"""Parental leave: what is declared, and the two things we can actually check.

Van Wyk's interim reading-in makes s25 one parent-neutral entitlement with two
totals (D-201):

- read-in s25(1): a single parent, or the only employed party in a parental
  relationship, gets at least four consecutive months
- read-in s25(4A): where both parties are employed, the parties are entitled IN
  THE AGGREGATE to four months and ten days

**The total itself depends on a fact we cannot see** (D-202). Whether the other
parent is employed is a fact about somebody who is not this employer's employee,
who may work for a company that has never heard of this product. So the
employer CAPTURES the declaration — the relationship shape and this employee's
share — and the system checks only what is checkable:

- a share ABOVE the declared shape's maximum is refused. That much follows from
  the declaration itself
- a share BELOW it is accepted in silence. The entitlement is a ceiling on what
  the employer must grant, not a floor on what the employee must take, and a
  system that argues with a mother who wants three months is wrong in a way she
  will remember
- s25(4B)'s single sequence IS enforceable for our own employee, and is
  enforced: one sequence per birth or placement, extensions welcome, a detached
  second period refused

**What is deliberately NOT built** (D-202): any notion of the other parent — no
partner field, no "both parents" record, no cross-employer lookup. And s25(4C)'s
"as close as possible to half" is applied by the PARTIES, not by this software:
it governs what they do when they cannot agree, and we never learn whether they
agreed.

**The event is a DATE and nothing else.** No child's name, no identity number:
none of it is needed for any calculation here, and a domestic employer's system
holding a child's identity number is a liability with no upside. The under-two
adoption limit is likewise a DECLARED yes/no, not a date of birth we store and
arithmetic over.
"""

from __future__ import annotations

import dataclasses
import datetime

from django.db import models

from leave.models import LeaveApplication, LeaveType

#: The leave types that draw on the one s25 entitlement. MATERNITY and ADOPTION
#: are sub-types of PARENTAL (``balance_source='parent'``, D-201), so the family
#: is the parent plus everything pointing at it.
FAMILY_CODES = frozenset(
    {LeaveType.Code.PARENTAL, LeaveType.Code.MATERNITY, LeaveType.Code.ADOPTION}
)


class RelationshipShape(models.TextChoices):
    """Which limb of the read-in s25 the employee falls under. DECLARED."""

    SINGLE_PARENT = "single_parent", "Single parent"
    ONLY_EMPLOYED_PARTY = "only_employed_party", "The only employed party"
    BOTH_EMPLOYED = "both_employed", "Both parties employed"


@dataclasses.dataclass(frozen=True)
class ParentalDeclaration:
    """What the employer captured, and cannot verify. Stored verbatim on the
    application, with who declared it and when, because if this is ever disputed
    the employer's defence is the record and not a recomputation."""

    shape: str
    share_months: int
    share_days: int
    event_date: datetime.date
    child_under_age_limit: bool | None = None


def is_parental(leave_type: LeaveType) -> bool:
    """A SYSTEM row of the parental family. A tenant's own row coded PARENTAL is
    its own scheme and none of this applies to it (D-127, D-192's own keying)."""
    return bool(leave_type.is_system) and leave_type.code in FAMILY_CODES


def maximum_for(shape: str, quantum) -> tuple[int, int]:
    """The ceiling in (months, days) for a declared shape. Read from the
    reference row — never a literal four or ten."""
    if shape == RelationshipShape.BOTH_EMPLOYED:
        return quantum.both_employed_months, quantum.both_employed_days
    return quantum.sole_parent_months, quantum.sole_parent_days


def describe(months: int, days: int) -> str:
    parts = []
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days or not parts:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return " and ".join(parts)


def exceeds_maximum(declaration: ParentalDeclaration, quantum) -> bool:
    """Compared as (months, days) rather than converted to a day count: the
    statute states months, and converting to days would need a calendar the
    comparison does not have (D-204)."""
    max_months, max_days = maximum_for(declaration.shape, quantum)
    return (declaration.share_months, declaration.share_days) > (max_months, max_days)


def sequence_for_event(employee, event_date: datetime.date):
    """Every application already recorded for this employee and this event,
    across the whole family — a maternity application and a parental one for the
    same birth are one sequence, not two (2c)."""
    return (
        LeaveApplication.objects.filter(employee=employee, parental_event_date=event_date)
        .exclude(status=LeaveApplication.Status.CANCELLED)
        .order_by("start_date")
    )


def detached_from(existing, *, start_date: datetime.date, end_date: datetime.date) -> bool:
    """s25(4B): each party's leave "must be taken by the party concerned in a
    single sequence of consecutive days". A span that touches or overlaps the
    sequence extends it; one with a working gap on either side is a second
    sequence, which the order does not permit.
    """
    day = datetime.timedelta(days=1)
    for application in existing:
        touches = (
            start_date <= application.end_date + day and end_date >= application.start_date - day
        )
        if touches:
            return False
    return True
