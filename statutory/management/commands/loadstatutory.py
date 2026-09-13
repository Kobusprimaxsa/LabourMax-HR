"""``python manage.py loadstatutory <fixture.json>`` — load one statutory data file.

The load half of decision D-51: the data load is human and verified, only its
application by effective date is automatic. This command is the human half's tool.

It refuses the whole file on any problem and writes nothing, so a failed load leaves
the database exactly as it was. On success it prints what it loaded, what was already
present, and every period it closed — which is the list the second person checks
during verification.

Nothing here marks the load as verified. That is deliberate, it is a different
command, and the database refuses to advance ``data_current_through`` without it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import AppUser
from statutory.loader import ReferenceDataLoadError, load_reference_file


class Command(BaseCommand):
    help = "Load a statutory reference data fixture. Refuses the whole file on any error."

    def add_arguments(self, parser):
        parser.add_argument("fixture", help="Path to the JSON fixture.")
        parser.add_argument(
            "--loaded-by",
            help="Email address of the person doing the load. Recorded on the version.",
        )

    def handle(self, *args, **options):
        loaded_by = None
        if options.get("loaded_by"):
            loaded_by = AppUser.objects.filter(email__iexact=options["loaded_by"]).first()
            if loaded_by is None:
                raise CommandError(f"No user with email {options['loaded_by']}.")

        try:
            report = load_reference_file(options["fixture"], loaded_by=loaded_by)
        except ReferenceDataLoadError as exc:
            raise CommandError(f"Load refused. Nothing was written.\n\n{exc}") from exc

        for line in report.lines():
            self.stdout.write(line)

        if report.closed_periods:
            self.stdout.write(
                self.style.WARNING(
                    "\nPeriods were closed by this load. Check each one against the source "
                    "document before verifying the version."
                )
            )
