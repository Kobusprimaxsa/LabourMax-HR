"""Submitting a leave application — P6 chunk 2, task 2.

**Evidence gates pay, not leave** (task 1's own rule, applied here). Nothing
in this module refuses an application for want of a certificate. BCEA s23
conditions the employer's right to WITHHOLD PAY on an unevidenced absence
beyond a threshold — it never conditions the right to TAKE the leave. A
sick application with no certificate, however long, is always created and
always reaches ``submitted``; only ``is_paid`` on its days is affected.

**An overdrawn application is never refused either** (Kobus's own framing).
The excess is capped against the ledger — never let a balance run negative
by paying what is not there — and the days beyond what the balance can
cover are marked ``is_paid=False`` and ``deducted_from_balance=False``:
they are taken, they are just unpaid. ``exceeds_balance`` carries the
overdraw; ``unpaid_days`` or ``unpaid_hours`` — whichever is the
application's own unit, the other staying zero — carries the whole unpaid
portion, for whatever reason a day is unpaid (D-188); ``leave/authorisation.py::approve()`` is
what actually warns and commits it to the ledger.

**A week's leave over a public holiday costs four days, not five —
UNLESS this employer's own ``PublicHolidayObservance`` says otherwise**
(P6 chunk 3, task 4). ``is_working_day`` is FALSE for a rest day AND for a
public holiday inside the span — neither deducts, neither is paid, and
both still get their own ``leave_application_day`` row so the audit can
show why. Which dates count as a holiday for THIS employer is decided by
``_is_observed_holiday()`` — an explicit observance row wins over the
statutory calendar in either direction; see that function and
``PublicHolidayObservance``'s own docstring for the compliance note this
carries.

FLAGGED: a ``balance_source='parent'`` type (``ANNUAL_UNAUTHORISED``) is not
resolved to its PARENT's cycle here — ``ensure_cycles``/``balance_as_at``
only ever generate or read a cycle for ``balance_source='own'`` types, so an
application against a parent-drawing type always sees a zero balance and is
treated as fully overdrawn. Genuinely applying for unauthorised leave
through this flow needs that resolution built; nothing in this chunk's own
task list exercises it, and inventing the resolution without a cited shape
to build it against would be guessing. Recorded rather than silently wrong.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from attendance import scheduling
from core.managers import tenant_context_of
from employees.models import EmployeeLeaveEntitlement
from leave.cycles import accrual_method_for, ensure_cycles, unit_for_method
from leave.evidence import SICK_CERTIFICATE_THRESHOLD_PARAMETER
from leave.models import (
    LeaveApplication,
    LeaveApplicationDay,
    LeaveCycle,
    LeaveEvidenceType,
    LeaveType,
    PublicHolidayObservance,
)
from statutory import resolve

ZERO = Decimal("0")
_QUANTUM = Decimal("0.001")
AccrualMethod = EmployeeLeaveEntitlement.AccrualMethod


class ApplicationRefusedError(Exception):
    """The application may not be created this way. Nothing was written."""


class ParentalLeaveRefusedError(ApplicationRefusedError):
    """The parental leave declaration is missing, or asks for more than the
    declared relationship shape allows, or breaks s25(4B)'s single sequence.
    Nothing was written."""


class FamilyResponsibilityIneligibleError(ApplicationRefusedError):
    """BCEA s27(1) does not apply to this employee on this date. Names the limb
    that failed and its figure. Nothing was written."""


def _next_reference(tenant) -> str:
    """``LV-{year}-{sequence}``, per tenant. Never reused.

    Counts this tenant's own applications this year and adds one. A genuine
    race between two submissions in the same instant is caught by
    ``uniq_leave_application_reference`` rather than prevented here — the
    same trade this codebase makes nowhere else needing a lock, because the
    volumes involved (one household or one cleaning firm's own leave
    requests) make the race vanishingly unlikely and the constraint means it
    can never silently double up.
    """
    year = timezone.localdate().year
    count = LeaveApplication.objects.filter(
        tenant=tenant, reference__startswith=f"LV-{year}-"
    ).count()
    return f"LV-{year}-{count + 1:05d}"


def _is_sick_leave_paid(
    leave_type: LeaveType,
    evidence_type: LeaveEvidenceType | None,
    *,
    working_units: Decimal,
    on_date: datetime.date,
) -> bool:
    """BCEA s23(1): independent evidence pays regardless of length; with
    none, the whole period is paid only within the statutory threshold.

    Raises if the threshold is not loaded — the same "resolve raises when
    missing" rule this codebase applies everywhere a statutory figure could
    otherwise be silently skipped rather than read.
    """
    if leave_type.code != LeaveType.Code.SICK:
        return True
    if evidence_type is not None and evidence_type.requires_attachment:
        return True

    threshold = resolve.parameter_value(SICK_CERTIFICATE_THRESHOLD_PARAMETER, on_date)
    return working_units <= threshold


def _is_observed_holiday(employer, a_date: datetime.date) -> bool:
    """Whether ``a_date`` is treated as a holiday for THIS employer — task
    4's observance override, checked before the statutory calendar.

    An explicit ``PublicHolidayObservance`` row wins outright, in EITHER
    direction: ``is_observed=False`` on a genuine statutory holiday means
    this employer's employees worked it as ordinary (see the model's own
    compliance note — this records an agreement, it does not make one), and
    an employer-specific row with no ``public_holiday`` at all can declare a
    day off the statutory calendar knows nothing about. No row falls back to
    the plain calendar exactly as chunk 2 read it.
    """
    observance = PublicHolidayObservance.objects.filter(
        employer=employer, observance_date=a_date
    ).first()
    if observance is not None:
        return observance.is_observed
    return resolve.is_public_holiday(a_date)


def _build_days(
    employee,
    leave_type: LeaveType,
    *,
    start_date: datetime.date,
    end_date: datetime.date,
    is_part_day: bool,
    unit: str,
) -> list[dict]:
    """The shape of every calendar date in the span — before pay or balance
    are known. One dict per date, in order.
    """
    days = []
    for a_date in scheduling.iter_dates(start_date, end_date):
        schedule = scheduling.current_schedule(employee, a_date)
        schedule_day = scheduling.schedule_day_for(schedule, a_date)
        is_public_holiday = _is_observed_holiday(employee.employer, a_date)
        is_working_day = bool(
            schedule_day is not None and schedule_day.is_working_day and not is_public_holiday
        )

        portion = Decimal("0.500") if is_part_day else Decimal("1.000")
        hours = None
        if is_working_day and unit == LeaveCycle.Unit.HOURS:
            scheduled_hours = schedule_day.ordinary_hours if schedule_day else ZERO
            hours = (scheduled_hours * portion).quantize(_QUANTUM, rounding=ROUND_HALF_UP)

        days.append(
            {
                "leave_date": a_date,
                "day_portion": portion,
                "hours": hours,
                "is_working_day": is_working_day,
                "is_public_holiday": is_public_holiday,
                "is_paid": True,
                "deducted_from_balance": is_working_day,
            }
        )
    return days


def _check_parental(employee, leave_type, declaration, *, start_date, end_date):
    """Everything the order lets us check, and nothing it does not (D-202).

    The quantum is resolved on the application's own start date, so when the
    interim reading-in lapses this refuses rather than falling back (D-203).
    """
    from leave import parental as parental_rules
    from statutory import resolve
    from statutory.resolve import StatutoryValueMissingError

    if declaration is None:
        raise ParentalLeaveRefusedError(
            f"{leave_type.code} leave needs the employer's declaration before it can be "
            f"captured: the relationship shape (single parent, the only employed party, or "
            f"both employed - read-in s25(1) and s25(4A) give different totals), this "
            f"employee's share in months and days, and the date of the birth, placement or "
            f"adoption order. The share cannot be checked against anything without it, "
            f"because whether the other parent is employed is not a fact this system can see."
        )

    try:
        quantum = resolve.parental_quantum(start_date)
    except StatutoryValueMissingError as missing:
        raise ParentalLeaveRefusedError(str(missing)) from missing

    if parental_rules.exceeds_maximum(declaration, quantum):
        months, days = parental_rules.maximum_for(declaration.shape, quantum)
        shape_label = parental_rules.RelationshipShape(declaration.shape).label
        asked = parental_rules.describe(declaration.share_months, declaration.share_days)
        raise ParentalLeaveRefusedError(
            f"A share of {asked} "
            f"is more than {shape_label.lower()} allows: "
            f"{parental_rules.describe(months, days)} "
            f"({quantum.source_reference}). Taking LESS than the maximum is fine and is not "
            f"refused - the entitlement is a ceiling on what the employer must grant, not a "
            f"floor on what the employee must take."
        )

    if leave_type.code == LeaveType.Code.ADOPTION:
        try:
            limit = resolve.adoption_age_limit(start_date)
        except StatutoryValueMissingError as missing:
            raise ParentalLeaveRefusedError(str(missing)) from missing
        if limit.is_limited and declaration.child_under_age_limit is False:
            raise ParentalLeaveRefusedError(
                f"Adoption leave is limited to a child below the age of "
                f"{limit.max_child_age_years} until {limit.effective_to:%d %B %Y} "
                f"(read-in s25B(1); {limit.source_reference}). The Constitutional Court has "
                f"already declared that limit invalid and suspended the declaration, so it "
                f"falls away on that date and this application would then be accepted."
            )

    existing = list(parental_rules.sequence_for_event(employee, declaration.event_date))
    if existing and parental_rules.detached_from(
        existing, start_date=start_date, end_date=end_date
    ):
        first, last = existing[0], existing[-1]
        raise ParentalLeaveRefusedError(
            f"Parental leave for the event of {declaration.event_date:%d %B %Y} is already "
            f"recorded from {first.start_date:%d %B %Y} to {last.end_date:%d %B %Y}, and "
            f"read-in s25(4B) requires it to be taken 'in a single sequence of consecutive "
            f"days'. Extend that sequence instead - a period starting the day after it ends "
            f"is the same sequence - or cancel it first."
        )

    return declaration


# ---------------------------------------------------------------------------
# THE TWO CAPS THAT ARE NOT BALANCES (D-269).
#
# PRENATAL and SHOP_STEWARD keep no balance at all (D-268): neither is a
# per-cycle bank, so there is nothing for the overdraw arithmetic above to read
# and nothing for a monthly run to add to. Their ceilings come from the
# instrument instead, exactly as parental leave's does (D-201) - and like
# parental leave, the excess is never refused. It falls to unpaid through the
# same columns every other unpaid reason uses (D-188), never a third mechanism.
#
# Refusing would be the wrong shape twice over. D-174 settled that an
# application is never refused for being overdrawn, and these are not even
# overdrawals: clause 13.2 grants three paid clinic days and says nothing about
# a fourth, so a fourth is ordinary unpaid time off that the employer may still
# allow. What the cap decides is PAY, never whether the leave may be taken.
# ---------------------------------------------------------------------------


class PrenatalDateRequiredError(ApplicationRefusedError):
    """A prenatal clinic day with no expected date of confinement to count against."""


def _scope_of(employee, on_date):
    """The employer's sector and this employee's wage area, most specific first."""
    from leave.cycles import _sector_area_of

    return employee.employer.sector, _sector_area_of(employee, on_date)


def prenatal_windows(
    expected_date_of_confinement: datetime.date, months_before_birth: int
) -> list[tuple[datetime.date, datetime.date]]:
    """The periods clause 13.2's "each of the 3 months prior to" names.

    **The three CALENDAR months before the month of confinement**, half-open
    like every other range here. A due date anywhere in December 2026 gives
    September, October and November — exactly three, whatever the day.

    **Counting one-month periods back from the due date was implemented first
    and rejected** (D-269), by a test rather than by argument. Those windows
    MOVE with the declared date: a due date revised by ten days shifts every
    boundary, so a month already paid for can fall outside the new window and
    a second paid day appears in the same calendar month. A revised due date is
    an ordinary event — that is what "expected" means — and it must not buy a
    fourth paid day. Calendar months do not move, so "one paid day per month"
    can be checked without knowing which pregnancy a day belongs to, and the
    three-day total falls out of it rather than needing a counter of its own.

    Both readings give exactly three days, so nothing is lost by taking the one
    that cannot be walked.
    """
    month_start = expected_date_of_confinement.replace(day=1)
    windows = []
    for index in range(months_before_birth, 0, -1):
        starts = month_start - relativedelta(months=index)
        windows.append((starts, starts + relativedelta(months=1)))
    return windows


def _paid_days_between(employee, leave_type, start, end) -> int:
    """Paid days of one leave type already standing between two dates.

    Counts SUBMITTED and APPROVED applications and ignores declined and
    cancelled ones, because a cancelled clinic day was not taken and must not
    consume the entitlement - the same reading ``leave/authorisation.py`` takes
    when it reverses a cancelled application's ledger row.
    """
    return LeaveApplicationDay.objects.filter(
        leave_application__employee=employee,
        leave_application__leave_type=leave_type,
        leave_application__status__in=[
            LeaveApplication.Status.SUBMITTED,
            LeaveApplication.Status.APPROVED,
        ],
        leave_date__gte=start,
        leave_date__lt=end,
        is_paid=True,
        is_working_day=True,
    ).count()


def _apply_prenatal_cap(employee, leave_type, day_dicts, *, expected_date_of_confinement):
    """One paid day in each of the three months before the due date, and no more.

    The per-month rule does the real work and needs no notion of WHICH
    pregnancy a day belongs to: a day is paid only if no paid prenatal day
    already stands in the same calendar month. So a revised due date cannot buy
    a second paid day in a month already used, which both a per-pregnancy
    counter and the date-relative windows this first used would have allowed.
    """
    sector, sector_area = _scope_of(employee, day_dicts[0]["leave_date"])
    entitlement = resolve.prenatal_clinic_leave(sector, day_dicts[0]["leave_date"], sector_area)

    if not entitlement.granted:
        # No instrument in force creates this leave for this employee. The days
        # stand as unpaid time off rather than being refused.
        for day_dict in day_dicts:
            if day_dict["is_working_day"]:
                day_dict["is_paid"] = False
        return

    windows = prenatal_windows(expected_date_of_confinement, entitlement.months_before_birth)
    used_in_window = {
        index: _paid_days_between(employee, leave_type, starts, ends)
        for index, (starts, ends) in enumerate(windows)
    }

    for day_dict in day_dicts:
        if not day_dict["is_working_day"]:
            continue
        index = next(
            (
                n
                for n, (starts, ends) in enumerate(windows)
                if starts <= day_dict["leave_date"] < ends
            ),
            None,
        )
        if index is None or used_in_window[index] >= entitlement.paid_days_per_month:
            day_dict["is_paid"] = False
            continue
        used_in_window[index] += 1


def _apply_shop_steward_cap(employee, leave_type, day_dicts):
    """Four or six paid days in the CALENDAR year, depending on the role held.

    **The year is the calendar year** (D-269). The clause says "per year" and
    does not say which; this agreement uses "Calendar Year" in terms at clause
    4.5(d) for the incentive bonus, which is the only place it defines a year
    at all, so that is the reading loaded. Flagged for the labour law review
    rather than asserted as settled (O-06) - an employment-anniversary year is
    the other defensible reading and would move which days are paid.

    **No role row means no entitlement**, which is the safe direction: an
    employee nobody has recorded as a shop steward is capped at nothing rather
    than at six days.
    """
    from employees.models import EmployeeUnionRole

    on_date = day_dicts[0]["leave_date"]
    role = (
        EmployeeUnionRole.objects.filter(employee=employee, effective_from__lte=on_date)
        .exclude(effective_to__lte=on_date)
        .order_by("-effective_from")
        .first()
    )
    sector, sector_area = _scope_of(employee, on_date)
    entitlement = (
        resolve.shop_steward_leave(
            sector,
            on_date,
            is_office_bearer=role.role == EmployeeUnionRole.Role.OFFICE_BEARER,
            sector_area=sector_area,
        )
        if role is not None
        else None
    )

    if entitlement is None or not entitlement.granted:
        for day_dict in day_dicts:
            if day_dict["is_working_day"]:
                day_dict["is_paid"] = False
        return

    year_start = datetime.date(on_date.year, 1, 1)
    year_end = datetime.date(on_date.year + 1, 1, 1)
    already = _paid_days_between(employee, leave_type, year_start, year_end)
    remaining = entitlement.days_per_year - already

    for day_dict in day_dicts:
        if not day_dict["is_working_day"]:
            continue
        if remaining <= 0 or not (year_start <= day_dict["leave_date"] < year_end):
            day_dict["is_paid"] = False
            continue
        remaining -= 1


def submit_application(
    employee,
    *,
    leave_type: LeaveType,
    start_date: datetime.date,
    end_date: datetime.date,
    reason: str = "",
    leave_evidence_type: LeaveEvidenceType | None = None,
    is_part_day: bool = False,
    submitted_by=None,
    parental=None,
    expected_date_of_confinement: datetime.date | None = None,
) -> LeaveApplication:
    """Create and submit a leave application. Atomic. Never refuses for want
    of evidence, and never refuses for being overdrawn — see the module
    docstring.

    ``is_part_day`` is only meaningful for a single-day application — half
    days are the minimum increment for a salaried basis, and this chunk does
    not attempt a half-day-in-the-middle-of-a-longer-span shape sheet 02
    does not ask for either.
    """
    if end_date < start_date:
        raise ApplicationRefusedError(
            f"{end_date:%d %B %Y} is before {start_date:%d %B %Y} — an application "
            f"cannot end before it starts."
        )
    if is_part_day and start_date != end_date:
        raise ApplicationRefusedError(
            "A part-day application must cover exactly one date. Half days are the "
            "minimum increment for a salaried basis; a multi-day half-day span is "
            "not a shape this chunk supports."
        )

    with transaction.atomic(), tenant_context_of(employee):
        if leave_type.code == LeaveType.Code.FAMILY_RESPONSIBILITY:
            from leave.eligibility import family_responsibility_eligibility

            eligibility = family_responsibility_eligibility(employee, start_date)
            if not eligibility.is_eligible:
                raise FamilyResponsibilityIneligibleError(
                    f"Family responsibility leave from {start_date:%d %B %Y} refused: "
                    + " ".join(eligibility.reasons)
                )

        if leave_type.code == LeaveType.Code.PRENATAL and expected_date_of_confinement is None:
            raise PrenatalDateRequiredError(
                "A prenatal clinic day must state the expected date of confinement it "
                "counts against. BCCCI clause 13.2 gives one paid day in each of the "
                "3 months BEFORE that date, so with no date there is no window to be "
                "inside and nothing caps the entitlement. It is declared and never "
                "computed: nothing in this system knows when a pregnancy is due."
            )

        from leave import parental as parental_rules

        parental_declaration = None
        if parental_rules.is_parental(leave_type):
            parental_declaration = _check_parental(
                employee, leave_type, parental, start_date=start_date, end_date=end_date
            )

        method, _entitlement = accrual_method_for(employee, leave_type, start_date)
        unit = unit_for_method(method)

        # D-195. An unauthorised absence is EITHER unpaid and uncharged (the
        # default) OR annual leave, charged and paid — the employer's election,
        # fixed into the day rows here so a later change cannot rewrite it.
        # Never both unpaid and charged (D-193). Keyed on the system row.
        unauthorised_treatment = None
        if leave_type.is_system and leave_type.code == LeaveType.Code.ANNUAL_UNAUTHORISED:
            from employers.onboarding import setting_value

            unauthorised_treatment = setting_value(
                employee.employer, "UNAUTHORISED_ABSENCE_TREATMENT"
            )

        ensure_cycles(employee, leave_type, horizon=start_date)
        from leave.balances import balance_as_at

        cycle = balance_as_at(employee, leave_type, start_date)
        available = cycle.balance_quantity if cycle is not None else ZERO

        day_dicts = _build_days(
            employee,
            leave_type,
            start_date=start_date,
            end_date=end_date,
            is_part_day=is_part_day,
            unit=unit,
        )

        quantity_field = "day_portion" if unit == LeaveCycle.Unit.DAYS else "hours"

        # D-196. Sick leave beyond the BCEA s23(1) threshold with no certificate
        # is UNPAID and NOT charged: the s22 entitlement is untouched. Decided
        # BEFORE the balance is read, so an uncertified day never draws on it
        # and never trips the overdraw either.
        working_units_for_sick = sum(
            (d[quantity_field] for d in day_dicts if d["is_working_day"]), ZERO
        )
        sick_is_paid = _is_sick_leave_paid(
            leave_type,
            leave_evidence_type,
            working_units=working_units_for_sick,
            on_date=start_date,
        )

        if parental_declaration is not None:
            # Parental leave accrues nothing and draws on nothing (D-201): the
            # ceiling is the declared share against the statutory quantum, not a
            # balance. Unpaid by default (read-in s25(7): payment is the UIF's
            # question, not the employer's), unless this employer has elected to
            # pay it by contract (D-205). Either way the days are recorded
            # through the SAME unpaid columns as every other unpaid reason
            # (D-188) - never a third mechanism.
            from employers.onboarding import setting_value

            employer_pays = bool(setting_value(employee.employer, "PARENTAL_LEAVE_PAID"))
            for day_dict in day_dicts:
                if day_dict["is_working_day"]:
                    day_dict["is_paid"] = employer_pays
                day_dict["deducted_from_balance"] = False

        # The two instrument-capped types (D-269). Applied BEFORE the overdraw
        # arithmetic, which reads a balance neither of them has, so `requested`
        # below sees only days that are still paid and `exceeds_balance` stays
        # false rather than reporting an overdraw against a zero balance.
        if leave_type.is_system and leave_type.code == LeaveType.Code.PRENATAL:
            _apply_prenatal_cap(
                employee,
                leave_type,
                day_dicts,
                expected_date_of_confinement=expected_date_of_confinement,
            )
            for day_dict in day_dicts:
                day_dict["deducted_from_balance"] = False
        elif leave_type.is_system and leave_type.code == LeaveType.Code.SHOP_STEWARD:
            _apply_shop_steward_cap(employee, leave_type, day_dicts)
            for day_dict in day_dicts:
                day_dict["deducted_from_balance"] = False

        if unauthorised_treatment == "unpaid" or not sick_is_paid:
            for day_dict in day_dicts:
                if day_dict["is_working_day"]:
                    day_dict["is_paid"] = False
                    day_dict["deducted_from_balance"] = False
        requested = sum((d[quantity_field] for d in day_dicts if d["deducted_from_balance"]), ZERO)

        exceeds_balance = requested > available
        unpaid_units = max(requested - available, ZERO) if exceeds_balance else ZERO

        # The excess falls to unpaid, working BACKWARD from the last day in
        # the span — never refused, never silently paid from a balance that
        # is not there (task 2).
        remaining_unpaid = unpaid_units
        for day_dict in reversed(day_dicts):
            if not day_dict["deducted_from_balance"] or remaining_unpaid <= 0:
                continue
            day_dict["is_paid"] = False
            day_dict["deducted_from_balance"] = False
            remaining_unpaid -= day_dict[quantity_field]

        if not leave_type.is_paid and unauthorised_treatment != "annual_leave":
            for day_dict in day_dicts:
                if day_dict["is_working_day"] and day_dict["deducted_from_balance"]:
                    day_dict["is_paid"] = False

        total_days = (
            sum((d["day_portion"] for d in day_dicts if d["is_working_day"]), ZERO)
            if unit == LeaveCycle.Unit.DAYS
            else ZERO
        )
        total_hours = (
            sum((d["hours"] or ZERO for d in day_dicts if d["is_working_day"]), ZERO)
            if unit == LeaveCycle.Unit.HOURS
            else None
        )

        unpaid_total = sum(
            (
                d[quantity_field] or ZERO
                for d in day_dicts
                if d["is_working_day"] and not d["is_paid"]
            ),
            ZERO,
        )

        application = LeaveApplication(
            tenant=employee.tenant,
            employee=employee,
            reference=_next_reference(employee.tenant),
            leave_type=leave_type,
            leave_evidence_type=leave_evidence_type,
            start_date=start_date,
            end_date=end_date,
            total_days=total_days,
            total_hours=total_hours,
            is_part_day=is_part_day,
            reason=reason,
            status=LeaveApplication.Status.SUBMITTED,
            submitted_by_user=submitted_by,
            submitted_at=timezone.now(),
            expected_date_of_confinement=expected_date_of_confinement,
            balance_at_submission=available,
            exceeds_balance=exceeds_balance,
            # D-188: the unpaid portion, in the application's own unit, for
            # EVERY reason a working day is unpaid — beyond the balance, a type
            # unpaid by nature, or sick pay withheld for want of evidence — not
            # only the overdraw. The other unit's column stays zero; nothing
            # here converts a day into hours (D-164).
            unpaid_days=unpaid_total if unit == LeaveCycle.Unit.DAYS else ZERO,
            unpaid_hours=unpaid_total if unit == LeaveCycle.Unit.HOURS else ZERO,
            parental_event_date=(parental_declaration.event_date if parental_declaration else None),
            parental_relationship_shape=(
                parental_declaration.shape if parental_declaration else ""
            ),
            parental_share_months=(
                parental_declaration.share_months if parental_declaration else None
            ),
            parental_share_days=(parental_declaration.share_days if parental_declaration else None),
            parental_child_under_age_limit=(
                parental_declaration.child_under_age_limit if parental_declaration else None
            ),
            parental_declared_by_user=submitted_by if parental_declaration else None,
            parental_declared_at=timezone.now() if parental_declaration else None,
        )
        application.full_clean()
        application.save()

        for day_dict in day_dicts:
            LeaveApplicationDay.objects.create(
                tenant=employee.tenant, leave_application=application, **day_dict
            )

    return application
