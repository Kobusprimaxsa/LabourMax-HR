"""Engaging and re-engaging an employee — the rules that are not columns.

Three things happen here rather than on the model, because each of them needs more
than one row to decide:

**The minimum age.** BCEA s43(1): a person must not require or permit a child to
work if the child is under 15, or under the minimum school-leaving age. Under
s43(3) a contravention is an **offence** committed by the employer — so this
function refuses rather than warns, and refuses rather than guessing when it cannot
establish the threshold.

The fifteen is **not written here**. It is a statutory threshold, so by CLAUDE.md's
rule it lives in ``statutory_parameter`` with its citation and is read through
``statutory.resolve`` (D-100). That rule is usually enforced by
``test_no_hardcoded_rates``, which scans for ``Decimal`` and ``float`` literals —
an age is an ``int``, so nothing automated would have caught a literal ``15``
sitting in this file. The rule is about statutory figures, not about a Python type.

**Re-engagement.** A returning employee gets a NEW engagement row, numbered one
higher, and never an edit to the old one. Service length counts from the engagement,
and an employee who left in 2027 and returned in 2029 has two periods rather than
one four-year period. Overwriting would hand them notice, leave and severance they
never earned, and every downstream figure would look entirely plausible.

**Closing the previous engagement.** ``is_current`` carries a partial unique
constraint, so the old engagement has to stop being current in the same transaction
that the new one starts — otherwise the insert fails on the constraint, which is the
right outcome but a confusing message.

**A termination captured in advance changes nothing until its date** (D-132).
``terminate()`` records the facts — the date, the reason, whether notice is worked —
and then asks ``employees/currentstate.py`` what is true *today*. An employee serving
a month's notice stays current, active and billable, because they are: the payroll
run that owes them a final salary must still find them, and the subscription is still
being used. What is open is therefore ``termination_date IS NULL``, not
``is_current`` — the two stopped meaning the same thing the day this changed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from core.managers import tenant_context_of
from employees.currentstate import refresh_current_state
from employees.identity import age_on
from employees.models import Employee, EmployeeEngagement, EmployeePosition
from statutory import resolve

#: The parameter that holds the figure. Named once so a typo is one grep away
#: rather than a lookup that silently raises "no such parameter".
MINIMUM_AGE_PARAMETER = "MINIMUM_EMPLOYMENT_AGE"


class EngagementRefusedError(Exception):
    """The engagement may not be created. Nothing was written."""


@dataclass(frozen=True)
class AgeCheck:
    """What the age rule decided, and on what figure."""

    permitted: bool
    age: int
    minimum_age: int
    reason: str = ""


def check_minimum_age(date_of_birth: datetime.date, start_date: datetime.date) -> AgeCheck:
    """BCEA s43(1), read against the reference data rather than against a literal.

    Raises ``EngagementRefusedError`` when the threshold cannot be established at all.
    That is deliberate and is the opposite of how ``PayGroup.clean()`` handles
    missing reference data, where validation is skipped because the staleness guard
    blocks the payroll run further along. There is no such backstop here, and the
    failure mode is not a wrong number on a payslip — it is a child in employment
    and an offence under s43(3). So: refuse, and say what to load.
    """
    try:
        minimum = int(resolve.parameter_value(MINIMUM_AGE_PARAMETER, start_date))
    except resolve.StatutoryValueMissingError as error:
        raise EngagementRefusedError(
            f"The minimum employment age is not loaded for {start_date:%d %B %Y}, so "
            f"this engagement cannot be checked against BCEA s43. Run "
            f"`python manage.py loadstatutory --all`. This refuses rather than "
            f"assuming a figure, because employing a child is an offence under "
            f"s43(3) and not a rounding error."
        ) from error

    age = age_on(date_of_birth, start_date)
    if age >= minimum:
        return AgeCheck(permitted=True, age=age, minimum_age=minimum)

    return AgeCheck(
        permitted=False,
        age=age,
        minimum_age=minimum,
        reason=(
            f"This person is {age} on {start_date:%d %B %Y}, and the minimum "
            f"employment age is {minimum}. BCEA s43(1) forbids the engagement and "
            f"s43(3) makes it an offence, so it cannot be recorded. If the date of "
            f"birth is wrong, correct it on the employee record first."
        ),
    )


def engage(
    employee: Employee,
    *,
    start_date: datetime.date,
    job_title: str,
    contract_type: str = EmployeeEngagement.ContractType.PERMANENT,
    fixed_term_end_date: datetime.date | None = None,
    job_grade=None,
    workplace=None,
    site_assignment: str = EmployeePosition.SiteAssignment.SINGLE_SITE,
    **engagement_fields,
) -> EmployeeEngagement:
    """Engage or re-engage an employee, with the opening position. Atomic.

    Returns the new engagement. Raises ``EngagementRefusedError`` and writes nothing if
    the age rule forbids it, if an engagement is already open, or if the start date
    falls inside a previous period of service.
    """
    check = check_minimum_age(employee.date_of_birth, start_date)
    if not check.permitted:
        raise EngagementRefusedError(check.reason)

    with transaction.atomic(), tenant_context_of(employee):
        existing = list(EmployeeEngagement.objects.filter(employee=employee))

        # Open means "no termination date", not "is_current" (D-132). An engagement
        # terminated with effect from next month is still current today and must not
        # read as open, or a re-hire captured in advance would be refused.
        open_engagement = next((e for e in existing if e.termination_date is None), None)
        if open_engagement is not None:
            raise EngagementRefusedError(
                f"{employee} is already engaged from "
                f"{open_engagement.start_date:%d %B %Y}. Terminate that engagement "
                f"before starting another — two open engagements would count service "
                f"twice for notice, leave and severance."
            )

        overlapping = [
            e
            for e in existing
            if e.termination_date is not None and start_date <= e.termination_date
        ]
        if overlapping:
            latest = max(e.termination_date for e in overlapping)
            raise EngagementRefusedError(
                f"A previous engagement ran to {latest:%d %B %Y}, so a new one cannot "
                f"start on {start_date:%d %B %Y}. Overlapping periods double-count "
                f"continuous service."
            )

        # An engagement that starts next month is not current today, and the partial
        # unique would refuse it anyway while the outgoing one still is (D-132).
        starts_today_or_earlier = start_date <= timezone.localdate()

        engagement = EmployeeEngagement.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement_number=max((e.engagement_number for e in existing), default=0) + 1,
            start_date=start_date,
            contract_type=contract_type,
            fixed_term_end_date=fixed_term_end_date,
            is_current=starts_today_or_earlier,
            **engagement_fields,
        )

        EmployeePosition.objects.create(
            tenant=employee.tenant,
            employee=employee,
            engagement=engagement,
            job_title=job_title,
            job_grade=job_grade,
            workplace=workplace,
            site_assignment=site_assignment,
            effective_from=start_date,
            change_reason=EmployeePosition.ChangeReason.NEW_ENGAGEMENT,
        )

        # One place decides what is true today, and this is not it (D-132). An
        # engagement starting next month leaves the employee in draft until it does.
        refresh_current_state(employee, on_date=timezone.localdate())

    return engagement


def terminate(
    engagement: EmployeeEngagement,
    *,
    termination_date: datetime.date,
    reason_code: str,
    notice_worked: bool | None = None,
    notes: str = "",
) -> EmployeeEngagement:
    """Record the end of an engagement. The employee stays; the service ends.

    The facts are written here — date, reason, whether notice is worked. What is
    *true today* is then recomputed by ``employees/currentstate.py``, which is the
    only place that decides it (D-132).

    So a termination dated today closes the engagement immediately, and one dated
    next month changes nothing today: the employee stays current, active and
    billable until the date arrives, and the nightly job moves them. That is what
    the flag was always documented to mean, and setting it false here was the bug —
    an employee serving notice would vanish from the payroll run that still owes
    them a final salary.
    """
    if termination_date < engagement.start_date:
        raise EngagementRefusedError(
            f"Employment cannot end on {termination_date:%d %B %Y}, before it started "
            f"on {engagement.start_date:%d %B %Y}."
        )
    if not reason_code:
        raise EngagementRefusedError(
            "A termination needs a reason: it decides whether severance is due under "
            "BCEA s41 and what goes on the UI-19."
        )

    with transaction.atomic(), tenant_context_of(engagement):
        engagement.termination_date = termination_date
        engagement.termination_reason_code = reason_code
        engagement.notice_worked = notice_worked
        engagement.termination_notes = notes
        engagement.save(
            update_fields=[
                "termination_date",
                "termination_reason_code",
                "notice_worked",
                "termination_notes",
                "updated_at",
            ]
        )

        # The position closes on the facts, not on today: effective_to is EXCLUSIVE,
        # so it ends the day AFTER the last day of service, or that final day has no
        # position and therefore no job grade to be paid against.
        EmployeePosition.objects.filter(engagement=engagement, effective_to__isnull=True).update(
            effective_to=termination_date + datetime.timedelta(days=1)
        )

        refresh_current_state(engagement.employee, on_date=timezone.localdate())
        engagement.refresh_from_db(fields=["is_current"])

    return engagement


def continuous_service_days(employee: Employee, on_date: datetime.date) -> int:
    """Days of service in the CURRENT engagement only.

    Named for what it is. The BCEA counts continuous service, and a break in
    employment breaks it — so a re-hired employee's notice period and leave accrual
    start again from the new engagement. Summing every engagement would be a
    different function with a different name, and it is not this one.
    """
    current = (
        EmployeeEngagement.objects.filter(employee=employee).order_by("-engagement_number").first()
    )
    if current is None:
        return 0
    return current.service_days_to(on_date)
