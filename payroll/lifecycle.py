"""The pay period's lifecycle — open → in progress → closed, and reopened.

The generator (P3, D-82 to D-85) makes periods; this module is the only thing
that MOVES one (D-305). Same shape as ``payroll/runs.py::transition()`` (D-233):
the legal moves are data, one function makes every move, and a test asserts every
status appears in the map, because a status added and forgotten here would be
treated as terminal by accident.

* **open** — generated, nothing run over it yet.
* **in_progress** — a run has been opened over it.
* **closed** — every run over it is finalised or reversed. A closed period
  refuses a new run: its pay is on documents people have been handed.
* **reopened** — a finalised run over it was REVERSED. The reversal is the
  audited act; the reason is on the reversal run's ``notes`` and the count is
  ``reopened_count``. Only ``runs.reverse()`` reopens a period, so a reopen
  always has a reason on record — there is no column on the period to hold one.

**A period does not close while any run over it is live.** ``close()`` refuses
and names the run. Finalising the LAST live run over a period closes it
(``runs.finalise()``, D-235 as amended by D-305): finalising a regular run while
the December bonus run over the same period is still being checked leaves the
period in progress rather than closing it under the bonus run.
"""

from __future__ import annotations

from django.utils import timezone

from core.managers import tenant_context_of
from payroll.models import PayPeriod, PayrollRun

PeriodStatus = PayPeriod.Status
RunStatus = PayrollRun.Status

#: Where a period may go from where it is. A state with no entry is terminal.
LEGAL_PERIOD_TRANSITIONS: dict[str, set[str]] = {
    PeriodStatus.OPEN: {PeriodStatus.IN_PROGRESS},
    PeriodStatus.IN_PROGRESS: {PeriodStatus.CLOSED},
    PeriodStatus.CLOSED: {PeriodStatus.REOPENED},
    # A reopened period is run again (the replacement after a reversal), or
    # closed as it stands when the reversal alone was the whole correction.
    PeriodStatus.REOPENED: {PeriodStatus.IN_PROGRESS, PeriodStatus.CLOSED},
}

#: A run in one of these has nothing left to happen to it.
SETTLED_RUN_STATUSES = (RunStatus.FINALISED, RunStatus.REVERSED)


class PeriodError(RuntimeError):
    """The period cannot go where it was asked to go."""


def transition(period: PayPeriod, to_status: str, **fields) -> PayPeriod:
    """The ONE place a period's status changes.

    Re-reads the status first: a caller holding ``run.pay_period`` may hold it
    from before ``begin()`` moved it, and checking a stale status would refuse a
    legal move, or allow an illegal one.
    """
    with tenant_context_of(period):
        period.refresh_from_db(fields=["status", "closed_at", "reopened_count"])
    allowed = LEGAL_PERIOD_TRANSITIONS.get(period.status, set())
    if to_status not in allowed:
        raise PeriodError(
            f"Period {period.period_number} cannot go from {period.status} to {to_status}. "
            f"From {period.status} it may go to "
            f"{', '.join(sorted(allowed)) or 'nowhere'}."
        )
    with tenant_context_of(period):
        period.status = to_status
        for name, value in fields.items():
            setattr(period, name, value)
        period.save(update_fields=["status", *fields, "updated_at"])
    return period


def live_runs(period: PayPeriod):
    with tenant_context_of(period):
        return list(period.runs.exclude(status__in=SETTLED_RUN_STATUSES).order_by("run_number"))


def begin(period: PayPeriod) -> PayPeriod:
    """A run is being opened over the period. Refuses a closed one."""
    with tenant_context_of(period):
        period.refresh_from_db(fields=["status", "closed_at", "reopened_count"])
    if period.status == PeriodStatus.CLOSED:
        raise PeriodError(
            f"Period {period.period_number} is closed. Its pay is on documents people have "
            f"been handed; a correction is a reversal of the run that paid it, which "
            f"reopens the period and counts that it did (pay_period.reopened_count)."
        )
    if period.status == PeriodStatus.IN_PROGRESS:
        return period
    return transition(period, PeriodStatus.IN_PROGRESS)


def close(period: PayPeriod, *, when=None) -> PayPeriod:
    """Close the period, or refuse while any run over it is not settled."""
    live = live_runs(period)
    if live:
        raise PeriodError(
            f"Period {period.period_number} cannot close while "
            + ", ".join(f"run {run.run_number} ({run.run_type}) is {run.status}" for run in live)
            + ". Finalise it, or reverse what it finalised, first."
        )
    return transition(period, PeriodStatus.CLOSED, closed_at=when or timezone.now())


def close_if_settled(period: PayPeriod, *, when=None) -> bool:
    """Close the period if nothing over it is still live. True if it closed."""
    if live_runs(period):
        return False
    close(period, when=when)
    return True


def reopen(period: PayPeriod) -> PayPeriod:
    """Only ``runs.reverse()`` calls this — the reversal run holds the reason."""
    return transition(
        period,
        PeriodStatus.REOPENED,
        closed_at=None,
        reopened_count=period.reopened_count + 1,
    )
