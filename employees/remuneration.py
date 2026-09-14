"""Capturing pay — the minimum wage check, and closing the previous rate.

P4's definition of done is here: *capturing an employee below the sectoral minimum
raises a visible, logged exception, and a future-dated increase flips the cache on
its own effective date.*

**The minimum wage is checked against the derived HOURLY rate**, because that is the
one figure both sides of the comparison share. ``minimum_wage_rate.hourly_rate`` is
the authoritative column in the gazette table and every captured basis normalises to
an hourly rate in ``employees/rates.py``. Comparing monthly-to-monthly instead would
mean recomputing the gazette's monthly figure from its hourly one, or trusting the
gazetted monthly column — which is stored as published and rounds.

**Which floor applies depends on three things**, and assembling them is why this is a
service rather than a ``clean()``: the employer's sector, the workplace's area, and
the position's job grade. A fourth — the ordinary-hours band — is passed in rather
than derived, and ``check_minimum_wage`` says why (D-105).

**Below the minimum is a refusal that can be accepted, not a block.** The workbook
makes ``is_below_minimum`` a flag with an acknowledging user beside it, and that is
right: an employer part-way through fixing a typo must not be locked out of their own
record. So the service raises, and takes an explicit ``acknowledged_by`` to proceed —
which is then stored, because "the system let me" is not a defence at the CCMA.

**Refusing when the floor cannot be established at all** is the same call as the
minimum-age check (D-101), and for the same reason: there is no backstop behind this
one. A payroll run has the staleness guard; a capture has nothing.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.managers import tenant_context_of
from employees import rates
from employees.models import (
    Employee,
    EmployeeEngagement,
    EmployeePosition,
    EmployeeRemuneration,
    WorkSchedule,
)
from statutory import resolve
from statutory.models import MinimumWageRate

#: BCEA s35's four and one-third. Named once, read from reference data (D-104).
MONTHLY_FACTOR_PARAMETER = "MONTHLY_TO_WEEKLY_FACTOR"


class RemunerationRefusedError(Exception):
    """The rate cannot be captured. Nothing was written."""


class BelowMinimumWageError(RemunerationRefusedError):
    """The rate is under the applicable floor and nobody has accepted that.

    Carries the figures so a screen can show both without asking again.
    """

    def __init__(self, message: str, *, offered: Decimal, floor: Decimal, rule: MinimumWageRate):
        super().__init__(message)
        self.offered = offered
        self.floor = floor
        self.rule = rule


@dataclass(frozen=True)
class MinimumWageCheck:
    """The floor that applied, the rate offered, and whether it clears."""

    rule: MinimumWageRate
    offered_hourly: Decimal
    floor_hourly: Decimal

    @property
    def clears(self) -> bool:
        return self.offered_hourly >= self.floor_hourly

    @property
    def shortfall(self) -> Decimal:
        return max(self.floor_hourly - self.offered_hourly, Decimal(0))


def _current_position(employee: Employee, on_date: datetime.date) -> EmployeePosition | None:
    return (
        EmployeePosition.objects.filter(employee=employee, effective_from__lte=on_date)
        .exclude(effective_to__lte=on_date)
        .order_by("-effective_from")
        .first()
    )


def _current_schedule(employee: Employee, on_date: datetime.date) -> WorkSchedule | None:
    return (
        WorkSchedule.objects.filter(employee=employee, effective_from__lte=on_date)
        .exclude(effective_to__lte=on_date)
        .order_by("-effective_from")
        .first()
    )


def band_for(employee: Employee, on_date: datetime.date) -> str:
    """The wage band from the employee's own work schedule, or ``all``.

    This is what D-105 was waiting for. The band is not computed by comparing hours
    against a threshold — it is read from ``work_schedule.works_over_27_hours_week``,
    which the employer declares. The gazette keeps its 27; nothing in this codebase
    needs to know it (D-110).

    An employee with no schedule captured yet resolves to ``all``, which falls back
    through ``statutory.resolve`` to the sector-wide row. That is the right default:
    the alternative is refusing to capture a rate until a schedule exists, and the
    two are captured on the same screen.
    """
    schedule = _current_schedule(employee, on_date)
    if schedule is None:
        return MinimumWageRate.HoursBand.ALL
    return schedule.hours_band


def check_minimum_wage(
    employee: Employee,
    *,
    hourly_rate: Decimal,
    on_date: datetime.date,
    hours_band: str | None = None,
) -> MinimumWageCheck:
    """The applicable floor on a date, and whether this rate clears it.

    Assembles the scope from three tables — the employer's sector, and the position's
    job grade and workplace area — then hands it to ``statutory.resolve``, which owns
    the fallback order. Nothing here decides which row wins.

    **``hours_band`` comes from the employee's work schedule when it is not given.**
    This module first computed the band by comparing hours against a literal
    ``Decimal(27)`` and the no-hard-coded-rate guard caught it — correctly, because 27
    is a gazetted threshold rather than arithmetic (D-105). The workbook's answer is
    better than a reference-data lookup would have been:
    ``work_schedule.works_over_27_hours_week`` is a boolean the **employer declares**,
    so the gazette keeps its threshold and no figure lives in code at all (D-110).
    """
    position = _current_position(employee, on_date)
    workplace = position.workplace if position else None

    try:
        rule = resolve.minimum_wage(
            on_date,
            sector=employee.employer.sector,
            sector_area=(workplace.sector_area if workplace else None)
            or employee.employer.sector_area,
            job_grade=position.job_grade if position else None,
            hours_band=hours_band if hours_band is not None else band_for(employee, on_date),
        )
    except resolve.StatutoryValueMissingError as error:
        raise RemunerationRefusedError(
            f"No minimum wage is loaded for {on_date:%d %B %Y}, so this rate cannot be "
            f"checked against the floor. Run `python manage.py loadstatutory --all`. "
            f"This refuses rather than capturing an unchecked rate, because underpaying "
            f"against the National Minimum Wage is an offence and the employer carries "
            f"it, not the system."
        ) from error

    return MinimumWageCheck(rule=rule, offered_hourly=hourly_rate, floor_hourly=rule.hourly_rate)


def capture(
    employee: Employee,
    *,
    pay_basis: str,
    rate_amount: Decimal,
    effective_from: datetime.date,
    pay_group=None,
    hours_per_day: Decimal | None = None,
    days_per_week: Decimal | None = None,
    hours_per_week: Decimal | None = None,
    change_reason: str = "",
    acknowledged_by=None,
    as_at: datetime.date | None = None,
) -> EmployeeRemuneration:
    """Capture a pay rate, closing whatever was in force before it. Atomic.

    Raises ``BelowMinimumWageError`` unless ``acknowledged_by`` is given, and writes
    nothing in that case. Raises ``RemunerationRefusedError`` if there is no open
    engagement, if the working pattern is unusable, or if the floor cannot be
    established at all.

    ``as_at`` is the date the **cache** is refreshed for, and defaults to today. It
    is NOT ``effective_from``, and the difference is the whole of D-18: a rate
    captured in March to start in July must leave ``employee.current_pay_basis``
    showing March's, or the employee list groups four months of payroll under a pay
    group that does not apply yet. Written the obvious way first — refreshing as at
    the new row's own effective date — this moved the cache the moment the row was
    saved, which is precisely the bug the nightly job exists to catch. Explicit
    rather than reading the clock inside, so a test can state the day it means.
    """
    with transaction.atomic(), tenant_context_of(employee):
        # Open means no termination date, not ``is_current`` (D-132). An employee
        # engaged from next Monday has no current engagement yet and their opening
        # rate is captured today, in the same sitting; reading ``is_current`` here
        # refused exactly that, which is most of what onboarding is.
        engagement = (
            EmployeeEngagement.objects.filter(employee=employee, termination_date__isnull=True)
            .order_by("-engagement_number")
            .first()
        )
        if engagement is None:
            raise RemunerationRefusedError(
                f"{employee} has no open engagement, so there is no period of "
                f"employment for this rate to belong to. Engage them first."
            )
        if effective_from < engagement.start_date:
            raise RemunerationRefusedError(
                f"A rate cannot take effect on {effective_from:%d %B %Y}, before the "
                f"engagement began on {engagement.start_date:%d %B %Y}."
            )

        group = pay_group or _pay_group_for(employee, engagement)
        pattern = rates.WorkingPattern(
            hours_per_day=hours_per_day
            if hours_per_day is not None
            else group.default_hours_per_day,
            days_per_week=days_per_week
            if days_per_week is not None
            else group.default_days_per_week,
            hours_per_week=hours_per_week
            if hours_per_week is not None
            else group.default_hours_per_day * group.default_days_per_week,
        )

        factor = _monthly_factor(effective_from)
        try:
            derived = rates.derive(pay_basis, rate_amount, pattern, factor)
        except rates.RateDerivationError as error:
            raise RemunerationRefusedError(str(error)) from error

        check = check_minimum_wage(employee, hourly_rate=derived.hourly, on_date=effective_from)
        if not check.clears and acknowledged_by is None:
            raise BelowMinimumWageError(
                f"{derived.hourly} an hour is below the {check.floor_hourly} an hour "
                f"minimum that applies to {employee} from {effective_from:%d %B %Y} "
                f"({check.rule.source_reference}). That is a shortfall of "
                f"{check.shortfall} an hour. Correct the rate, or record who is "
                f"accepting it — underpaying is the employer's offence and the "
                f"acknowledgement is what shows who decided.",
                offered=derived.hourly,
                floor=check.floor_hourly,
                rule=check.rule,
            )

        _close_open_rate(employee, effective_from)

        row = EmployeeRemuneration.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement=engagement,
            pay_group=group,
            pay_basis=pay_basis,
            rate_amount=rate_amount,
            derived_hourly_rate=derived.hourly,
            derived_daily_rate=derived.daily,
            derived_monthly_rate=derived.monthly,
            hours_per_day=pattern.hours_per_day,
            days_per_week=pattern.days_per_week,
            hours_per_week=pattern.hours_per_week,
            minimum_wage_rate=check.rule,
            is_below_minimum=not check.clears,
            below_minimum_ack_by_user=acknowledged_by if not check.clears else None,
            effective_from=effective_from,
            change_reason=change_reason,
        )

        refresh_pay_cache(employee, on_date=as_at or timezone.localdate())

    return row


def _pay_group_for(employee: Employee, engagement: EmployeeEngagement):
    from employers.models import PayGroup

    group = PayGroup.objects.filter(employer=employee.employer, is_active=True).first()
    if group is None:
        raise RemunerationRefusedError(
            f"{employee.employer} has no active pay group, so there is no calendar to "
            f"pay this employee on. Create one before capturing a rate."
        )
    return group


def _monthly_factor(on_date: datetime.date) -> Decimal:
    try:
        return resolve.parameter_value(MONTHLY_FACTOR_PARAMETER, on_date)
    except resolve.StatutoryValueMissingError as error:
        raise RemunerationRefusedError(
            f"The monthly-to-weekly factor (BCEA s35) is not loaded for "
            f"{on_date:%d %B %Y}, so a rate cannot be normalised. Run "
            f"`python manage.py loadstatutory --all`."
        ) from error


def _close_open_rate(employee: Employee, on_date: datetime.date):
    """End the rate in force the day the new one starts.

    ``effective_to`` is EXCLUSIVE, so the old row ends on the new row's start date
    rather than the day before it. Getting that backwards leaves either a one-day gap
    with no rate at all, or a one-day overlap that the exclusion constraint refuses —
    and the second is the lucky outcome, because the first is silent.
    """
    EmployeeRemuneration.objects.filter(
        employee=employee, effective_to__isnull=True, effective_from__lt=on_date
    ).update(effective_to=on_date)


def rate_in_force(employee: Employee, on_date: datetime.date) -> EmployeeRemuneration | None:
    """The rate governing a date. Half-open, exactly like every statutory lookup."""
    return (
        EmployeeRemuneration.objects.filter(employee=employee, effective_from__lte=on_date)
        .exclude(effective_to__lte=on_date)
        .order_by("-effective_from")
        .first()
    )


def refresh_pay_cache(employee: Employee, *, on_date: datetime.date) -> bool:
    """Bring ``employee.current_pay_group`` and ``current_pay_basis`` up to date.

    D-18. Called on write here, and by a nightly job — because a future-dated
    increase has to flip on its own effective date with nobody touching the record,
    and a write-only cache would still be showing the old pay group on the morning
    the new rate starts.

    Returns whether anything changed, so the nightly job can report a count rather
    than "done".
    """
    row = rate_in_force(employee, on_date)
    group_id = row.pay_group_id if row else None
    basis = row.pay_basis if row else ""

    if employee.current_pay_group_id == group_id and employee.current_pay_basis == basis:
        return False

    employee.current_pay_group_id = group_id
    employee.current_pay_basis = basis
    employee.save(update_fields=["current_pay_group", "current_pay_basis", "updated_at"])
    return True
