"""The employee's as-at columns, and the job that moves them when a date arrives.

Five columns on `employee` and one flag on `employee_engagement` answer the same
question — *what is true about this person today* — from rows that were written
weeks earlier. They are caches in the sense of invariant 3: every one of them is
recomputable from the effective-dated rows behind it, and none of them is ever the
source of anything.

| Column | Recomputed from |
|---|---|
| `employee.current_pay_group` / `current_pay_basis` | the `employee_remuneration` row in force |
| `employee_engagement.is_current` | the engagement in force |
| `employee.status` | whether an engagement is in force, unless the status is
  one an engagement does not decide |
| `employee.is_billable` | the status, per D-33 |
| `employee.first_engagement_date` / `latest_termination_date` | min and max over
  the engagements — facts, not as-at figures |

**D-132: a termination captured in advance does not move any of them until the
termination date arrives.** `terminate()` used to set `is_current` false,
`status` to terminated and `is_billable` false at the moment of capture, which
said an employee serving a month's notice had already left. The model's own
docstring said the opposite — *"a future-dated termination is captured in advance
and the employee is still currently employed until it arrives"* — and the
consequences were not cosmetic: a payroll run that filters on the current
engagement would drop the person it still owes a final salary, and billing would
stop counting someone who is still at work. It is D-107 pointing the other way:
the cache moved on capture instead of on the date.

So the whole set moves here, on a date, and `terminate()` records facts. The
engagement CHECK constraint that forbade `is_current` alongside a termination date
went with it (migration `0006`): a future-dated termination is now precisely the
case where both are true, and the invariant that matters — at most one current
engagement per employee — is the partial unique, which still holds because
`engage()` refuses overlapping periods.

**Order matters when the flag moves between two engagements.** The partial unique
means the outgoing engagement must stop being current *before* the incoming one
starts, in the same transaction — so the falses are written first.

**What this job must not touch.** `on_leave`, `suspended` and `archived` are set
by the leave engine, the disciplinary file and the retention policy. An engagement
in force says nothing about which of them applies, so they are left exactly as
they are; only the draft / active / terminated transitions belong to the
engagement record.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from core.managers import tenant_context, tenant_context_of
from core.models import BackgroundJob, Tenant
from employees.models import Employee, EmployeeEngagement
from employees.remuneration import refresh_pay_cache

#: The job's name in `background_job`, so a missed night is visible without
#: reading broker internals.
JOB_NAME = "employees.refresh_current_state"

#: Statuses the engagement record does not decide. An employee on leave or
#: suspended is employed — the engagement in force says nothing about which —
#: and archived is terminal.
STATUSES_AN_ENGAGEMENT_DOES_NOT_DECIDE = frozenset(
    {
        Employee.Status.ON_LEAVE,
        Employee.Status.SUSPENDED,
        Employee.Status.ARCHIVED,
    }
)


@dataclass(frozen=True)
class StateChange:
    """What moved for one employee, so the job reports counts rather than "done"."""

    employee_id: int
    fields: tuple[str, ...] = ()
    engagements_moved: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.fields) or bool(self.engagements_moved)


@dataclass(frozen=True)
class RunSummary:
    """What a whole run did. Written to `background_job.result_summary`."""

    as_at: datetime.date
    tenants: int = 0
    employees: int = 0
    employees_changed: int = 0
    engagements_moved: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "as_at": self.as_at.isoformat(),
            "tenants": self.tenants,
            "employees": self.employees,
            "employees_changed": self.employees_changed,
            "engagements_moved": self.engagements_moved,
            "dry_run": self.dry_run,
        }


def is_in_force(engagement: EmployeeEngagement, on_date: datetime.date) -> bool:
    """Has this period of employment started, and not yet ended, on this date?

    `termination_date` is the **last day of service** and therefore inclusive —
    an employee terminated on the 31st is still employed on the 31st. Reading it
    as exclusive loses the person their final day of pay, and the day would look
    unworked rather than unpaid.
    """
    if engagement.start_date > on_date:
        return False
    return engagement.termination_date is None or engagement.termination_date >= on_date


def engagement_in_force(employee: Employee, on_date: datetime.date) -> EmployeeEngagement | None:
    """The engagement governing a date, or None.

    At most one can match: `engage()` refuses a second open engagement and refuses
    a start date inside a previous period, so the periods cannot overlap.

    Pins the tenant itself. Without it the query returns no rows and reads as "this
    person has never been engaged" rather than as "you did not say whose data this
    is" — the first row of CLAUDE.md's table.
    """
    with tenant_context_of(employee):
        for engagement in EmployeeEngagement.objects.filter(employee=employee).order_by(
            "-start_date"
        ):
            if is_in_force(engagement, on_date):
                return engagement
    return None


def status_as_at(
    employee: Employee,
    *,
    in_force: EmployeeEngagement | None,
    has_ended: bool,
) -> str:
    """The status the engagement record implies, or the one already set.

    `has_ended` means a period of service has finished on or before the date —
    which is what separates "terminated" from "engaged, but not started yet".
    """
    if employee.status in STATUSES_AN_ENGAGEMENT_DOES_NOT_DECIDE:
        if in_force is None and has_ended and employee.status != Employee.Status.ARCHIVED:
            # Employment ended while they were on leave or suspended. Service is
            # over either way, and leaving them suspended would keep billing them.
            return Employee.Status.TERMINATED
        return employee.status

    if in_force is not None:
        return Employee.Status.ACTIVE
    if has_ended:
        return Employee.Status.TERMINATED
    return Employee.Status.DRAFT


def refresh_current_state(employee: Employee, *, on_date: datetime.date) -> StateChange:
    """Bring every as-at column on this employee up to date. Idempotent.

    Called by the nightly job, and directly by `terminate()` so a termination
    dated today takes effect immediately rather than at midnight.

    Opens its own tenant context, so it is safe to call with nothing pinned. It
    does **not** open a transaction of its own: the job wraps a whole tenant in
    one, and a transaction per employee would multiply the commits by the
    headcount.
    """
    with tenant_context_of(employee):
        engagements = list(
            EmployeeEngagement.objects.filter(employee=employee).order_by("engagement_number")
        )

        in_force = next((e for e in engagements if is_in_force(e, on_date)), None)
        has_ended = any(
            e.termination_date is not None and e.termination_date <= on_date for e in engagements
        )

        # The falses first: the partial unique will not have two current
        # engagements even for the instant between two UPDATEs in one transaction.
        moved = 0
        incoming = None
        for engagement in engagements:
            should_be_current = in_force is not None and engagement.pk == in_force.pk
            if engagement.is_current == should_be_current:
                continue
            if should_be_current:
                incoming = engagement
                continue
            engagement.is_current = False
            engagement.save(update_fields=["is_current", "updated_at"])
            moved += 1
        if incoming is not None:
            incoming.is_current = True
            incoming.save(update_fields=["is_current", "updated_at"])
            moved += 1

        changed: list[str] = []

        first_engagement = min((e.start_date for e in engagements), default=None)
        if employee.first_engagement_date != first_engagement:
            employee.first_engagement_date = first_engagement
            changed.append("first_engagement_date")

        latest_termination = max(
            (e.termination_date for e in engagements if e.termination_date is not None),
            default=None,
        )
        if employee.latest_termination_date != latest_termination:
            employee.latest_termination_date = latest_termination
            changed.append("latest_termination_date")

        status = status_as_at(employee, in_force=in_force, has_ended=has_ended)
        if employee.status != status:
            employee.status = status
            changed.append("status")

        # D-33 states which statuses count toward the subscription headcount, on
        # the model. Deriving from it rather than repeating the rule is what stops
        # the two disagreeing on the day somebody adds a status.
        is_billable = status in Employee.BILLABLE_STATUSES
        if employee.is_billable != is_billable:
            employee.is_billable = is_billable
            changed.append("is_billable")

        if changed:
            employee.save(update_fields=[*changed, "updated_at"])

        if refresh_pay_cache(employee, on_date=on_date):
            changed.extend(["current_pay_group", "current_pay_basis"])

    return StateChange(employee_id=employee.pk, fields=tuple(changed), engagements_moved=moved)


def refresh_tenant(tenant_id: int, *, on_date: datetime.date, dry_run: bool = False) -> RunSummary:
    """Every employee of one tenant, in one transaction.

    One transaction per tenant rather than one for the run: a failure on tenant
    forty must not roll back the thirty-nine that were already correct, and one
    transaction per employee would commit once per head every night.

    **The transaction opens first, and the context inside it** (D-92):
    `set_config(..., true)` is transaction-local, so under autocommit — which is
    where a management command runs — the tenant would be gone by the time of the
    first write.
    """
    with transaction.atomic(), tenant_context(tenant_id):
        employees = list(Employee.objects.all())
        changed = 0
        moved = 0
        for employee in employees:
            if dry_run:
                # Read-only: work out what would move, write nothing. The rollback
                # below would undo the writes anyway, but a dry run that writes
                # still fires the audit signals and burns the ids.
                state = _would_change(employee, on_date)
            else:
                state = refresh_current_state(employee, on_date=on_date)
            if state.changed:
                changed += 1
            moved += state.engagements_moved

        summary = RunSummary(
            as_at=on_date,
            tenants=1,
            employees=len(employees),
            employees_changed=changed,
            engagements_moved=moved,
            dry_run=dry_run,
        )

    return summary


def _would_change(employee: Employee, on_date: datetime.date) -> StateChange:
    """What `refresh_current_state` would do, without doing it."""
    engagements = list(EmployeeEngagement.objects.filter(employee=employee))
    in_force = next((e for e in engagements if is_in_force(e, on_date)), None)
    has_ended = any(
        e.termination_date is not None and e.termination_date <= on_date for e in engagements
    )

    moved = sum(
        1 for e in engagements if e.is_current != (in_force is not None and e.pk == in_force.pk)
    )

    fields: list[str] = []
    if employee.first_engagement_date != min((e.start_date for e in engagements), default=None):
        fields.append("first_engagement_date")
    if employee.latest_termination_date != max(
        (e.termination_date for e in engagements if e.termination_date is not None), default=None
    ):
        fields.append("latest_termination_date")

    status = status_as_at(employee, in_force=in_force, has_ended=has_ended)
    if employee.status != status:
        fields.append("status")
    if employee.is_billable != (status in Employee.BILLABLE_STATUSES):
        fields.append("is_billable")

    from employees.remuneration import rate_in_force

    row = rate_in_force(employee, on_date)
    if employee.current_pay_group_id != (row.pay_group_id if row else None) or (
        employee.current_pay_basis != (row.pay_basis if row else "")
    ):
        fields.extend(["current_pay_group", "current_pay_basis"])

    return StateChange(employee_id=employee.pk, fields=tuple(fields), engagements_moved=moved)


def refresh_all(
    *,
    on_date: datetime.date | None = None,
    tenant_ids: list[int] | None = None,
    dry_run: bool = False,
    record_job: bool = True,
) -> RunSummary:
    """The nightly run: every tenant, one transaction each.

    Iterates tenants and enters each one's context rather than reaching across
    them with `platform_context()`. D-54: a strict tenant table has no platform
    override at all, so there is nothing to reach across with — and a maintenance
    job that could see every employer at once is exactly the hole that function
    exists to keep singular.
    """
    on_date = on_date or timezone.localdate()

    # `tenant` is a platform table and `background_job` is tenant-optional, so
    # both are read and written with no tenant pinned — which is the only place
    # the NULL-tenant rows are visible.
    tenant_query = Tenant.objects.all()
    if tenant_ids is not None:
        tenant_query = tenant_query.filter(pk__in=tenant_ids)
    ids = list(tenant_query.values_list("pk", flat=True))

    job = None
    if record_job:
        job = BackgroundJob.objects.create(
            job_name=JOB_NAME,
            status=BackgroundJob.Status.RUNNING,
            started_at=timezone.now(),
            parameters={
                "as_at": on_date.isoformat(),
                "tenants": tenant_ids,
                "dry_run": dry_run,
            },
        )

    total = RunSummary(as_at=on_date, dry_run=dry_run)
    try:
        for tenant_id in ids:
            one = refresh_tenant(tenant_id, on_date=on_date, dry_run=dry_run)
            total = RunSummary(
                as_at=on_date,
                tenants=total.tenants + 1,
                employees=total.employees + one.employees,
                employees_changed=total.employees_changed + one.employees_changed,
                engagements_moved=total.engagements_moved + one.engagements_moved,
                dry_run=dry_run,
            )
    except Exception as error:
        if job is not None:
            job.status = BackgroundJob.Status.FAILED
            job.finished_at = timezone.now()
            job.error_message = f"{type(error).__name__}: {error}"
            job.result_summary = total.as_dict()
            job.save(update_fields=["status", "finished_at", "error_message", "result_summary"])
        raise

    if job is not None:
        job.status = BackgroundJob.Status.SUCCEEDED
        job.finished_at = timezone.now()
        job.result_summary = total.as_dict()
        job.save(update_fields=["status", "finished_at", "result_summary"])

    return total
