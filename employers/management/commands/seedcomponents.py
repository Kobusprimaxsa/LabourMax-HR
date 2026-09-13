"""``python manage.py seedcomponents`` — stock the shared payroll component catalogue.

A command rather than a data migration, for the same reason ``loadstatutory`` is:
a fresh clone has empty reference tables, and fourteen of the sixteen components
point at a SARS source code that has to exist first. A data migration would run at
the wrong moment and either fail the deploy or create components with no code.

Idempotent. A component already in the catalogue is left exactly as it is — the
system-row lock would refuse the write in any case, and a catalogue row that a
finalised payslip line points at must not change under it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.managers import platform_context
from employers.components import (
    SYSTEM_COMPONENTS,
    ComponentSeedError,
    seed_system_components,
)
from employers.models import PayrollComponent


class Command(BaseCommand):
    help = "Create any missing system payroll component. Never updates an existing one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--activate-severance",
            action="store_true",
            help=(
                "Create SEVERANCE active. Only once source code 3901 is loaded and "
                "the tax directive handling exists — see employers/components.py."
            ),
        )
        parser.add_argument("--list", action="store_true", help="Show the catalogue and stop.")

    def handle(self, *args, **options):
        if options["list"]:
            self._list()
            return

        try:
            created = seed_system_components(activate_severance=options["activate_severance"])
        except ComponentSeedError as error:
            raise CommandError(str(error)) from error

        # atomic() first, then the context block: set_config is transaction-local,
        # and a command runs under autocommit. See core/managers.py.
        with transaction.atomic(), platform_context():
            total = PayrollComponent.objects.shared().count()
            inactive = sorted(
                PayrollComponent.objects.shared()
                .filter(is_active=False)
                .values_list("code", flat=True)
            )

        for component in created:
            self.stdout.write(f"  + {component.code:<16} {component.name}")

        if not created:
            self.stdout.write("Nothing to create — the catalogue is already stocked.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Created {len(created)} components."))

        self.stdout.write(
            f"The shared catalogue holds {total} of {len(SYSTEM_COMPONENTS)} components."
        )
        if inactive:
            self.stdout.write(
                self.style.WARNING(
                    f"Inactive and unusable until their reference data exists: "
                    f"{', '.join(inactive)}. See employers/components.py for why."
                )
            )

    def _list(self):
        for spec in SYSTEM_COMPONENTS:
            flag = "" if spec.is_active else "  [INACTIVE]"
            code = spec.source_code or "—"
            self.stdout.write(
                f"{spec.display_order:>4}  {spec.code:<16} {code:<6} "
                f"{spec.component_type:<22} {spec.name}{flag}"
            )
