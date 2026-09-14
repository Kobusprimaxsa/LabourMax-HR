"""``python manage.py seeddocumentcategories`` — stock the shared document category catalogue.

A command rather than a data migration, for the same reason ``seedcomponents``
is: a fresh clone has empty reference tables, and two categories point at a
sector that has to exist first. A data migration would run at the wrong moment.

Idempotent. A category already in the catalogue is left exactly as it is — the
system-row lock would refuse the write in any case, and a tenant may already
have documents filed against it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.managers import platform_context
from documents.categories import (
    SYSTEM_CATEGORIES,
    CategorySeedError,
    seed_system_categories,
)
from documents.models import DocumentCategory


class Command(BaseCommand):
    help = "Create any missing system document category. Never updates an existing one."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="Show the catalogue and stop.")

    def handle(self, *args, **options):
        if options["list"]:
            self._list()
            return

        try:
            created = seed_system_categories()
        except CategorySeedError as error:
            raise CommandError(str(error)) from error

        # atomic() first, then the context block: set_config is transaction-local,
        # and a command runs under autocommit. See core/managers.py.
        with transaction.atomic(), platform_context():
            total = DocumentCategory.objects.shared().count()

        for cat in created:
            self.stdout.write(f"  + {cat.code:<32} {cat.name}")

        if not created:
            self.stdout.write("Nothing to create — the catalogue is already stocked.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Created {len(created)} categories."))

        self.stdout.write(
            f"The shared catalogue holds {total} of {len(SYSTEM_CATEGORIES)} categories."
        )

    def _list(self):
        for spec in SYSTEM_CATEGORIES:
            flags = []
            if spec.requires_expiry_date:
                flags.append("expires")
            if spec.visible_to_employee:
                flags.append("employee-visible")
            if spec.is_required_for_onboarding:
                flags.append("onboarding")
            if spec.sector_code:
                flags.append(spec.sector_code)
            flag_text = f"  [{', '.join(flags)}]" if flags else ""
            line = f"{spec.sort_order:>4}  {spec.code:<32} {spec.applies_to:<10} {spec.name}"
            self.stdout.write(line + flag_text)
