"""Leave an employer grants BEYOND the statute, recorded as exactly that (D-318).

BCEA s27 family responsibility leave applies only to an employee employed longer
than four months AND working at least four days a week (s27(1); SD7 clause 21(1)
the same for domestic workers, five days rather than three). That is a FLOOR.
An employer may always give more - to a part-time domestic worker who is a
household's only employee, or more than five days to somebody the Act covers -
and before this the software could not record it: the application refused, and
the only route was unauthorised absence, which charged annual leave and wrote an
act of generosity into the register as misconduct.

**Its own leave type, its own balance.** ``FAMILY_RESPONSIBILITY_CONTRACTUAL``
(is_statutory=False) carries its own cycle and ledger, so a contractual day is
never booked against the statutory five: an ineligible employee has no statutory
cycle at all, and an employer who later withdraws the policy closes a contractual
grant, never a statutory right.

**The balance comes from a GRANT, not from accrual.** ``grant()`` writes an
``employee_leave_entitlement`` row - per employee, effective-dated, already
overlap-guarded - naming who granted it, why, how many days a cycle and whether
they are paid (both are lawful and both happen), and posts that cycle's days in
one ledger row whose ``calculation_basis`` says ``contractual_grant``. The
accrual engine posts later cycles the same way while the grant is in force.

**The statutory rule does not move.** ``leave/accrual.py``'s family
responsibility path still grants nothing until both s27(1) limbs hold, and an
application for statutory family responsibility leave by somebody it does not
cover is still REFUSED by default, naming the limb and its figure.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from django.db import transaction

from core.managers import tenant_context_of
from employees.models import EmployeeLeaveEntitlement
from leave.models import LeaveType

CONTRACTUAL_CODE = LeaveType.Code.FAMILY_RESPONSIBILITY_CONTRACTUAL
CONTRACTUAL_BASIS = "contractual_grant"


class GrantRefusedError(ValueError):
    """A contractual grant is missing what makes it a grant."""


@dataclasses.dataclass(frozen=True)
class BeyondStatute:
    """Who decided to grant leave the law does not require, and why (D-318).
    Stored on the application; the same shape as D-108's acknowledgement."""

    authorised_by: object
    reason: str


def contractual_type() -> LeaveType:
    row = LeaveType.objects.shared().filter(code=CONTRACTUAL_CODE, is_system=True).first()
    if row is None:
        raise GrantRefusedError(
            f"The {CONTRACTUAL_CODE} leave type is not seeded. Run "
            f"`python manage.py seedleavetypes`."
        )
    return row


def grant_in_force(employee, on_date: datetime.date) -> EmployeeLeaveEntitlement | None:
    """The contractual grant in force on a date. Caller pins the tenant."""
    from leave.cycles import current_entitlement

    return current_entitlement(employee, contractual_type(), on_date)


def grant(
    employee,
    *,
    days_per_cycle: Decimal,
    is_paid: bool,
    granted_by,
    reason: str,
    effective_from: datetime.date,
) -> EmployeeLeaveEntitlement:
    """Grant contractual family responsibility leave and post this cycle's days."""
    if granted_by is None:
        raise GrantRefusedError(
            "A contractual grant names the person who granted it. The law does not "
            "require this leave, so somebody decided to give it."
        )
    if not reason.strip():
        raise GrantRefusedError(
            "A contractual grant states why - the policy, the contract clause or the "
            "decision - so the register can say it was a choice and not a right."
        )
    if days_per_cycle <= 0:
        raise GrantRefusedError(f"A grant of {days_per_cycle} day(s) grants nothing.")

    from leave.accrual import accrue_employee

    leave_type = contractual_type()
    with transaction.atomic(), tenant_context_of(employee):
        row = EmployeeLeaveEntitlement(
            tenant=employee.tenant,
            employee=employee,
            leave_type=leave_type,
            additional_days_per_cycle=days_per_cycle,
            accrual_method=EmployeeLeaveEntitlement.AccrualMethod.UPFRONT_ANNUAL,
            is_paid=is_paid,
            granted_by_user=granted_by,
            grant_reason=reason.strip(),
            effective_from=effective_from,
        )
        row.full_clean()
        row.save()
        accrue_employee(employee, leave_type, as_at=effective_from)
    return row
