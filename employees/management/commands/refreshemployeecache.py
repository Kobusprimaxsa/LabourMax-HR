"""``python manage.py refreshemployeecache`` — move the as-at columns onto today.

The other half of D-18, and the enforcement of D-132. Every column it touches is
maintained on write as well; this is what moves them when **a date arrives and
nobody touches the record** — the future-dated increase that starts on 1 July, the
termination captured a month in advance, the engagement booked for next Monday.

Run it nightly, early, before anybody opens the employee list:

```
python manage.py refreshemployeecache                 # today, every tenant
python manage.py refreshemployeecache --as-at 2026-07-01
python manage.py refreshemployeecache --tenant 12 --dry-run
```

``--as-at`` is for catching up after a missed night and for testing; it never reads
the clock itself. Running it twice changes nothing the second time.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError

from employees.currentstate import refresh_all


class Command(BaseCommand):
    help = "Recompute the employee current-state cache columns as at a date."

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-at",
            dest="as_at",
            help="ISO date to compute as at. Defaults to today in Africa/Johannesburg.",
        )
        parser.add_argument(
            "--tenant",
            dest="tenants",
            action="append",
            type=int,
            help="Limit to this tenant id. Repeatable. Default: every tenant.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would move and write nothing.",
        )
        parser.add_argument(
            "--no-job-record",
            action="store_true",
            help="Do not write a background_job row. For tests and ad-hoc runs.",
        )

    def handle(self, *args, **options):
        on_date = None
        if options["as_at"]:
            try:
                on_date = datetime.date.fromisoformat(options["as_at"])
            except ValueError as error:
                raise CommandError(
                    f"--as-at must be an ISO date such as 2026-07-01, not {options['as_at']!r}."
                ) from error

        summary = refresh_all(
            on_date=on_date,
            tenant_ids=options["tenants"],
            dry_run=options["dry_run"],
            record_job=not options["no_job_record"],
        )

        prefix = "Would move" if summary.dry_run else "Moved"
        self.stdout.write(f"As at {summary.as_at:%d %B %Y}, across {summary.tenants} tenant(s):")
        self.stdout.write(f"  {summary.employees} employees read")
        self.stdout.write(f"  {prefix} {summary.employees_changed} employee record(s)")
        self.stdout.write(f"  {prefix} {summary.engagements_moved} engagement flag(s)")

        if summary.employees_changed == 0 and summary.engagements_moved == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to move — every cache is current."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
