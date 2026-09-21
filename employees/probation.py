"""Probation — reading the one column that records it, and capping it (D-277).

``employee_engagement.probation_end_date`` has existed since P4's own second
migration and **nothing has ever read it**. That stopped being harmless when
BCCCI clause 21.1(b) turned out to answer notice differently for an employee
still on probation: the column is now the fact that decides between one week's
notice and two, and a column nothing reads is a column nobody fills in.

Two things live here, and they are deliberately separate:

**Is this employee on probation on a date.** A plain reading of the column, with
its boundary stated rather than assumed — see ``is_on_probation``.

**May this probation be this long.** BCCCI clause 3 caps it at four months for
contract cleaning Area B. The four is NOT written here: it is a statutory
threshold, so by CLAUDE.md's rule it lives in ``statutory_parameter`` with its
citation and is read through ``statutory.resolve`` (D-100's own precedent, and
D-100's own gap — ``test_no_hardcoded_rates`` scans for ``Decimal`` and
``float``, and a month count is an ``int``).

**The cap refuses rather than warns**, and it refuses only where an instrument
states one. Most employers are under no such cap and must not be told they are;
an employer who IS under one and captures five months has captured a term the
agreement does not permit, on the day they capture it, and the wrong moment to
discover that is at termination when the notice period is being computed from
it.
"""

from __future__ import annotations

import datetime

from dateutil.relativedelta import relativedelta

from statutory import resolve

#: Clause 3's maximum, per instrument that states one. Absent for every sector
#: whose instrument is silent, which is all of them but contract cleaning
#: Area B — and ``resolve.parameter_or_none`` answering None is the difference
#: between "no cap" and "a cap of zero" (D-198's shape, one table along).
PROBATION_CAP_PARAMETER = "PROBATION_MAX_MONTHS"


class ProbationRefusedError(ValueError):
    """The probation period is longer than the governing instrument allows."""


def is_on_probation(engagement, on_date: datetime.date) -> bool:
    """Is this engagement still on probation on ``on_date``?

    **The last day of probation is a day ON probation**, which is a reading and
    not a fact the column carries: ``probation_end_date`` is named for the end
    of the period rather than the day after it, the way ``termination_date`` is
    "the last day of service" three fields below it in the same model. Stated
    here so the one place that decides it can be found and argued with — the
    alternative convention moves a whole day's worth of employees between a
    one-week and a two-week notice band.

    No row and no date means NOT on probation. An employer who never captured a
    probation period did not put the employee on one, and inferring a default
    probation from silence would hand an employee the shorter notice period on
    the strength of a blank field.
    """
    if engagement is None or engagement.probation_end_date is None:
        return False
    return engagement.start_date <= on_date <= engagement.probation_end_date


def cap_months(on_date: datetime.date, *, sector=None, sector_area=None):
    """The maximum probation the governing instrument allows, or None.

    None means the instrument states no cap — not a cap of zero, and not "four
    months because that is usual". Nothing in the BCEA caps probation at all;
    the LRA's Code of Good Practice asks only that it be reasonable, which is
    not a number and must not be invented as one.
    """
    row = resolve.parameter_or_none(
        PROBATION_CAP_PARAMETER, on_date, sector=sector, sector_area=sector_area
    )
    return row


def check_probation(
    *,
    start_date: datetime.date,
    probation_end_date: datetime.date | None,
    sector=None,
    sector_area=None,
) -> None:
    """Refuse a probation period longer than the instrument permits.

    Writes nothing and returns nothing; raises ``ProbationRefusedError`` naming
    both dates, the maximum and the clause it comes from, so the message is the
    whole answer and the employer does not have to go looking for the figure.
    """
    if probation_end_date is None:
        return
    if probation_end_date < start_date:
        raise ProbationRefusedError(
            f"Probation ends {probation_end_date:%d %B %Y}, before the engagement "
            f"starts on {start_date:%d %B %Y}."
        )

    row = cap_months(start_date, sector=sector, sector_area=sector_area)
    if row is None or row.value_numeric is None:
        return

    months = int(row.value_numeric)
    latest = start_date + relativedelta(months=months)
    if probation_end_date > latest:
        raise ProbationRefusedError(
            f"Probation may run to {latest:%d %B %Y} at the latest — {months} months "
            f"from {start_date:%d %B %Y} — and {probation_end_date:%d %B %Y} is longer. "
            f"{row.source_reference}."
        )
