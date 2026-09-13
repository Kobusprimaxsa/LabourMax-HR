"""The staleness guard — decision D-50.

The failure this prevents: a payroll run for April 2027 executes against February
2026's rates because nobody loaded the new gazette, produces plausible-looking
payslips, and the error surfaces months later as corrected IRP5s and reversal runs
across every tenant at once.

The system's position is that it would rather refuse than guess. A run whose period
ends after ``reference_data_version.data_current_through`` raises a **blocking**
issue. Not a warning — a warning in a payroll UI is a thing people learn to click
past, and the whole point is that this one cannot be clicked past.

Why this is not just "use the newest row". Effective-dated tables answer "what was
the rate on this date" perfectly well, and will happily return February 2026's row
for a date in 2028 because it is still the newest row with no end date. That is
correct behaviour for the table and the wrong answer for a payslip. Only a
human-confirmed "we have checked the data is right up to here" can tell the
difference between "unchanged" and "unchecked", and that is what this reads.
"""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError

from statutory.models import ReferenceDataVersion


class ReferenceDataStaleError(ValidationError):
    """Raised instead of computing a payroll run against unverified rates.

    The message is written to be read by an employer, not a developer: it says
    what is wrong and who can fix it, because the person who hits this cannot fix
    it themselves.
    """


def current_version(on_date: datetime.date | None = None) -> ReferenceDataVersion | None:
    """The verified, golden-tested reference version in force on a date."""
    from django.utils import timezone

    return ReferenceDataVersion.in_force_on(on_date or timezone.localdate())


def assert_reference_data_covers(period_end: datetime.date, *, what: str = "This payroll run"):
    """Gate a payroll run on confirmed statutory data. Raises or returns the version.

    Called by the payroll run state machine in P7 before any calculator executes.
    It lives here rather than there so the rule is stated once, next to the data
    it is about.
    """
    version = current_version(period_end)

    if version is None:
        raise ReferenceDataStaleError(
            f"{what} cannot be processed: no verified statutory reference data applies to "
            f"{period_end:%d %B %Y}. Labourmax must load and verify the rates for this "
            f"period before payroll can run."
        )

    if version.data_current_through is None:
        raise ReferenceDataStaleError(
            f"{what} cannot be processed: reference version {version.version_label} has not "
            f"been confirmed correct up to any date. Labourmax must complete verification "
            f"before payroll can run."
        )

    if period_end > version.data_current_through:
        raise ReferenceDataStaleError(
            f"{what} covers a period ending {period_end:%d %B %Y}, but statutory rates are "
            f"only confirmed correct to {version.data_current_through:%d %B %Y}. Rates may "
            f"have changed since. Labourmax must load and verify the current rates before "
            f"this period can be processed."
        )

    return version
