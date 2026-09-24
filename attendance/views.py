"""The monthly attendance capture grid — the first screen (P5, D-299).

One employer, one pay group, one month. Every write goes through the services
that already existed: ``capture()`` for a cell, ``bulk_fill()`` for a week or a
month, ``approval.approve()`` for approval. There is no second write path.

**Every record comes from the URL, resolved inside the pinned tenant.** The
employer and the pay group by ``public_uid``; the employee by ``public_uid``
AND membership of the group for the month; the day by its number within the
month in the URL. Nothing here reads an id from the form body — the only
thing a form carries is WHAT was typed. Another tenant's uid resolves to
nothing, and nothing is a 404.

**A save that fails is visible on its own cell.** Each cell posts on its own
and the response is that cell re-rendered: saved, or marked with the refusal in
words — a locked day naming its payroll run, an approved day, a code that does
not parse, reference data not loaded. A failure the server never answered
(network, a 500) is caught in ``static/js/grid.js`` and marked on the cell too.
"""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from attendance import approval, cellcodes, scheduling
from attendance.capture import AttendanceCaptureRefusedError, capture
from attendance.grid import _month_bounds, _prefill_for, bulk_fill, month_grid
from attendance.models import AttendanceDay
from calculators.attendance import Severity
from core.web import employer_view, tenant_object
from employees.models import Employee, EmployeeRemuneration
from employers.models import Employer, PayGroup
from statutory import resolve
from statutory.resolve import StatutoryValueMissingError


def _month(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(f"{text}-01")
    except ValueError as error:
        raise Http404 from error


def employees_on(group: PayGroup, start: datetime.date, end: datetime.date) -> list[Employee]:
    """Everybody with a remuneration row on this group during the month, and an
    engagement overlapping it — the same population a payroll run over the
    month would pay."""
    ids = (
        EmployeeRemuneration.objects.filter(pay_group=group, effective_from__lte=end)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=start))
        .filter(engagement__start_date__lte=end)
        .filter(
            Q(engagement__termination_date__isnull=True)
            | Q(engagement__termination_date__gte=start)
        )
        .values_list("employee_id", flat=True)
    )
    return list(Employee.objects.filter(pk__in=list(ids)).order_by("last_name", "first_name", "pk"))


class Scope:
    """The employer, group and month named by the URL, all resolved and checked."""

    def __init__(self, employer_uid, group_uid, month):
        self.employer = tenant_object(Employer.objects, public_uid=employer_uid)
        self.group = tenant_object(PayGroup.objects, public_uid=group_uid, employer=self.employer)
        self.start, self.end = _month_bounds(_month(month))
        self.month = self.start.strftime("%Y-%m")
        self.employees = employees_on(self.group, self.start, self.end)

    def employee(self, employee_uid) -> Employee:
        found = next((e for e in self.employees if str(e.public_uid) == str(employee_uid)), None)
        if found is None:
            raise Http404
        return found

    def date(self, day: int) -> datetime.date:
        try:
            chosen = self.start.replace(day=day)
        except ValueError as error:
            raise Http404 from error
        if chosen > self.end:
            raise Http404
        return chosen

    def url(self, name, **extra):
        return reverse(
            f"attendance:{name}",
            kwargs={
                "employer_uid": self.employer.public_uid,
                "group_uid": self.group.public_uid,
                "month": self.month,
                **extra,
            },
        )


# ------------------------------------------------------------------ cells


def _cell_context(
    scope, employee, work_date, *, day, prefill=None, typed=None, error=None, saved=False
):
    """One cell. The caller supplies the day and the proposal it already has —
    the grid gets both from ``month_grid()`` in one pass, so rendering a month
    is not a query per empty cell."""
    return {
        "scope": scope,
        "employee": employee,
        "work_date": work_date,
        "day": day,
        "value": typed if typed is not None else (cellcodes.shown(day) if day else ""),
        "placeholder": _proposal_text(prefill),
        "error": error,
        "saved": saved,
        "editable": day is None
        or (
            day.status != AttendanceDay.Status.LOCKED
            and day.day_type != AttendanceDay.DayType.LEAVE
        ),
        "post_url": scope.url("cell", employee_uid=employee.public_uid, day=work_date.day),
    }


def _proposal_text(prefill) -> str:
    if prefill is None:
        return ""
    if prefill.time_in and prefill.time_out:
        return "O"
    return cellcodes.LETTERS.get(prefill.day_type, "")


def _save(employee, work_date, typed: str, *, replace_approved: bool):
    """Parse and capture ONE cell, inside its own savepoint so a refusal does
    not poison the request's transaction. Returns (day, error)."""
    try:
        entry = cellcodes.parse(
            typed, work_date=work_date, is_public_holiday=resolve.is_public_holiday(work_date)
        )
    except cellcodes.CellCodeError as refused:
        return None, str(refused)

    kwargs = {"day_type": entry.day_type, "is_standby": entry.is_standby}
    if entry.use_schedule:
        schedule = scheduling.current_schedule(employee, work_date)
        schedule_day = scheduling.schedule_day_for(schedule, work_date)
        if not (schedule_day and schedule_day.start_time and schedule_day.end_time):
            return None, "No scheduled times for this day. Type the hours worked instead: 9."
        kwargs.update(
            time_in=schedule_day.start_time,
            time_out=schedule_day.end_time,
            unpaid_break_minutes=schedule_day.unpaid_break_minutes,
        )
    elif entry.hours is not None:
        kwargs["hours_worked"] = entry.hours

    try:
        with transaction.atomic():
            day = capture(
                employee,
                work_date=work_date,
                source=AttendanceDay.Source.MANUAL,
                allow_replacing_approved=replace_approved,
                **kwargs,
            )
    except AttendanceCaptureRefusedError as refused:
        return None, str(refused)
    except ValidationError as refused:
        return None, "; ".join(refused.messages)
    except StatutoryValueMissingError as missing:
        return None, f"Cannot bucket this day: {missing}"
    except DatabaseError as refused:
        # The locked-day trigger, for any path capture()'s own check missed.
        return None, f"{work_date:%d %B %Y} could not be saved: {str(refused).splitlines()[0]}"
    return day, None


@employer_view
@require_POST
def cell(request, employer_uid, group_uid, month, employee_uid, day):
    scope = Scope(employer_uid, group_uid, month)
    employee = scope.employee(employee_uid)
    work_date = scope.date(day)
    typed = request.POST.get("value", "")
    saved, error = _save(
        employee, work_date, typed, replace_approved=request.POST.get("replace") == "approved"
    )
    day = saved or AttendanceDay.objects.filter(employee=employee, work_date=work_date).first()
    prefill = (
        _prefill_for(employee, work_date)
        if day is None and not scope.group.is_attendance_driven
        else None
    )
    context = _cell_context(
        scope,
        employee,
        work_date,
        day=day,
        prefill=prefill,
        typed=typed if error else None,
        error=error,
        saved=not error,
    )
    response = render(request, "attendance/_cell.html", context)
    if not error:
        response["HX-Trigger"] = "cellSaved"
    return response


# ------------------------------------------------------------------- grid


def _exceptions(scope, grid):
    """Blocking and warning exceptions, each with WHOSE it is."""
    blocking, warnings = [], []
    for row in grid.rows:
        name = f"{row.employee.first_name} {row.employee.last_name}"
        for exception in row.exceptions:
            target = blocking if exception.severity == Severity.BLOCKING else warnings
            target.append({"name": name, "exception": exception})
    return {"blocking": blocking, "warnings": warnings}


@employer_view
@require_GET
def grid(request, employer_uid, group_uid, month):
    scope = Scope(employer_uid, group_uid, month)
    built = month_grid(scope.employees, scope.start, pay_group=scope.group)
    dates = list(scheduling.iter_dates(scope.start, scope.end))
    holidays = set(
        resolve.public_holidays_between(scope.start, scope.end).values_list(
            "holiday_date", flat=True
        )
    )
    rows = [
        {
            "employee": row.employee,
            "cells": [
                _cell_context(
                    scope, row.employee, cell.work_date, day=cell.day, prefill=cell.prefill
                )
                for cell in row.cells
            ],
        }
        for row in built.rows
    ]
    previous = (scope.start - datetime.timedelta(days=1)).strftime("%Y-%m")
    following = (scope.end + datetime.timedelta(days=1)).strftime("%Y-%m")
    return render(
        request,
        "attendance/grid.html",
        {
            "scope": scope,
            "dates": dates,
            "holidays": holidays,
            "rows": rows,
            "legend": [
                (letter, day_type.label, day_type.value)
                for day_type, letter in cellcodes.LETTERS.items()
            ],
            "previous_url": reverse(
                "attendance:grid",
                kwargs={
                    "employer_uid": scope.employer.public_uid,
                    "group_uid": scope.group.public_uid,
                    "month": previous,
                },
            ),
            "next_url": reverse(
                "attendance:grid",
                kwargs={
                    "employer_uid": scope.employer.public_uid,
                    "group_uid": scope.group.public_uid,
                    "month": following,
                },
            ),
            **_exceptions(scope, built),
        },
    )


@employer_view
@require_GET
def exceptions(request, employer_uid, group_uid, month):
    scope = Scope(employer_uid, group_uid, month)
    built = month_grid(scope.employees, scope.start, pay_group=scope.group)
    return render(
        request, "attendance/_exceptions.html", {"scope": scope, **_exceptions(scope, built)}
    )


# ----------------------------------------------------------- bulk actions


def _span(scope, span: str) -> list[datetime.date]:
    if span == "month":
        return list(scheduling.iter_dates(scope.start, scope.end))
    if span.startswith("week:"):
        first = scope.date(int(span.split(":", 1)[1]))
        monday = first - datetime.timedelta(days=first.weekday())
        return [
            d
            for d in scheduling.iter_dates(monday, monday + datetime.timedelta(days=6))
            if scope.start <= d <= scope.end
        ]
    raise Http404


@employer_view
@require_POST
def bulk(request, employer_uid, group_uid, month):
    """Fill EMPTY cells only, through ``bulk_fill()``, on the employee's own
    scheduled working days. ``schedule`` fills each day at its scheduled times;
    a typed code fills every scheduled working day with that code."""
    scope = Scope(employer_uid, group_uid, month)
    who = request.POST.get("who", "all")
    targets = scope.employees if who == "all" else [scope.employee(who)]
    dates = _span(scope, request.POST.get("span", "month"))
    code = (request.POST.get("value") or "").strip()
    if scope.group.is_attendance_driven and code.lower() in ("", "schedule"):
        # D-25: an hourly or daily day must be captured deliberately. Filling a
        # month with scheduled hours nobody looked at is the invented wage D-25
        # refuses to pre-fill, done in one click instead of by default.
        messages.error(
            request,
            "This group is paid by the hour or day, so its hours are typed, not filled from "
            "the schedule (D-25). Bulk fill accepts a day-type letter here: R, A, P, N or H.",
        )
        return HttpResponse(status=204, headers={"HX-Refresh": "true"})

    filled, skipped = 0, []
    for employee in targets:
        working = []
        for work_date in dates:
            schedule = scheduling.current_schedule(employee, work_date)
            schedule_day = scheduling.schedule_day_for(schedule, work_date)
            if schedule_day is not None and schedule_day.is_working_day:
                working.append((work_date, schedule_day))
        if not working:
            skipped.append(f"{employee.first_name} {employee.last_name}: no scheduled working days")
            continue
        try:
            with transaction.atomic():
                filled += _bulk_one(employee, working, code)
        except (cellcodes.CellCodeError, AttendanceCaptureRefusedError, ValidationError) as refused:
            text = (
                "; ".join(refused.messages)
                if isinstance(refused, ValidationError)
                else str(refused)
            )
            skipped.append(f"{employee.first_name} {employee.last_name}: {text}")

    messages.success(
        request, f"{filled} empty day(s) filled. Captured days were left as they were."
    )
    for line in skipped:
        messages.warning(request, line)
    return HttpResponse(status=204, headers={"HX-Refresh": "true"})


def _bulk_one(employee, working, code: str) -> int:
    count = 0
    if code.lower() in ("", "schedule"):
        for work_date, schedule_day in working:
            if not (schedule_day.start_time and schedule_day.end_time):
                raise cellcodes.CellCodeError(
                    "the schedule has no start and end times to fill from"
                )
            count += len(
                bulk_fill(
                    employee,
                    [work_date],
                    day_type=cellcodes._inferred(work_date, resolve.is_public_holiday(work_date)),
                    time_in=schedule_day.start_time,
                    time_out=schedule_day.end_time,
                    unpaid_break_minutes=schedule_day.unpaid_break_minutes,
                )
            )
        return count
    by_type: dict[tuple, list[datetime.date]] = {}
    for work_date, _ in working:
        entry = cellcodes.parse(
            code, work_date=work_date, is_public_holiday=resolve.is_public_holiday(work_date)
        )
        if entry.use_schedule or entry.hours is not None:
            raise cellcodes.CellCodeError(
                "bulk fill takes 'schedule' or a day-type letter (R, A, P, N, H); hours "
                "differ day to day and are typed into the cells"
            )
        by_type.setdefault((entry.day_type,), []).append(work_date)
    for (day_type,), dates in by_type.items():
        count += len(bulk_fill(employee, dates, day_type=day_type))
    return count


@employer_view
@require_POST
def approve(request, employer_uid, group_uid, month):
    """Approve every CAPTURED day of the month for the group, or refuse and
    name each blocking exception (``approval.approve``)."""
    scope = Scope(employer_uid, group_uid, month)
    days = list(
        AttendanceDay.objects.filter(
            employee__in=scope.employees,
            work_date__gte=scope.start,
            work_date__lte=scope.end,
            status=AttendanceDay.Status.CAPTURED,
        ).order_by("employee_id", "work_date")
    )
    if not days:
        messages.info(request, "Nothing captured is waiting for approval.")
    else:
        try:
            with transaction.atomic():
                approval.approve(days, request.user)
            messages.success(request, f"{len(days)} day(s) approved.")
        except approval.ApprovalRefusedError as refused:
            messages.error(request, str(refused))
        except DatabaseError as refused:
            messages.error(
                request, f"Approval refused by the database: {str(refused).splitlines()[0]}"
            )
    return HttpResponse(status=204, headers={"HX-Refresh": "true"})
