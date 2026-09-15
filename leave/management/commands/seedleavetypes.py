"""``python manage.py seedleavetypes`` — stock the shared leave type catalogue.

Per machine, idempotent, in the ``seedcomponents`` mould (D-127 shipped
``leave_type`` empty deliberately, because seeding it is a compliance reading
that belongs with the engine honouring it — this chunk is that engine).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from core.managers import platform_context
from leave.evidence import FLAGGED_CODES as EVIDENCE_FLAGGED_CODES
from leave.evidence import SYSTEM_EVIDENCE_TYPES, seed_system_evidence_types
from leave.models import LeaveEvidenceType, LeaveType
from leave.types import FLAGGED_CODES, SYSTEM_LEAVE_TYPES, seed_system_leave_types


class Command(BaseCommand):
    help = "Create any missing system leave type or evidence type. Never updates an existing one."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="Show the catalogue and stop.")

    def handle(self, *args, **options):
        if options["list"]:
            self._list()
            return

        created = seed_system_leave_types()

        # atomic() first, then the context block: set_config is transaction-local,
        # and a command runs under autocommit. See core/managers.py, and D-92.
        with transaction.atomic(), platform_context():
            total = LeaveType.objects.filter(tenant__isnull=True).count()

        for leave_type in created:
            self.stdout.write(f"  + {leave_type.code:<24} {leave_type.name}")

        if not created:
            self.stdout.write("Nothing to create — the catalogue is already stocked.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Created {len(created)} leave types."))

        self.stdout.write(
            f"The shared catalogue holds {total} of {len(SYSTEM_LEAVE_TYPES)} leave types."
        )
        if FLAGGED_CODES:
            self.stdout.write(
                self.style.WARNING(
                    f"Shape flagged rather than settled, see leave/types.py: "
                    f"{', '.join(FLAGGED_CODES)}."
                )
            )

        evidence_created = seed_system_evidence_types()
        evidence_total = LeaveEvidenceType.objects.count()

        for evidence_type in evidence_created:
            self.stdout.write(f"  + {evidence_type.code:<24} {evidence_type.name}")

        if not evidence_created:
            self.stdout.write("Nothing to create — the evidence catalogue is already stocked.")
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Created {len(evidence_created)} evidence types.")
            )

        self.stdout.write(
            f"The evidence catalogue holds {evidence_total} of "
            f"{len(SYSTEM_EVIDENCE_TYPES)} evidence types."
        )
        if EVIDENCE_FLAGGED_CODES:
            self.stdout.write(
                self.style.WARNING(
                    f"Shape flagged rather than settled, see leave/evidence.py: "
                    f"{', '.join(EVIDENCE_FLAGGED_CODES)}."
                )
            )

    def _list(self):
        for spec in SYSTEM_LEAVE_TYPES:
            flag = "  [FLAGGED]" if spec.flagged else ""
            parent = f" -> {spec.parent_code}" if spec.parent_code else ""
            self.stdout.write(
                f"{spec.display_order:>4}  {spec.code:<24}{parent:<14} "
                f"{spec.balance_source:<8} cycle={spec.cycle_months:>3}mo{flag}"
            )
        self.stdout.write("")
        for spec in SYSTEM_EVIDENCE_TYPES:
            flag = "  [FLAGGED]" if spec.flagged else ""
            self.stdout.write(
                f"{spec.display_order:>4}  {spec.code:<24} "
                f"paid_by_default={spec.is_paid_by_default!s:<5} "
                f"attachment={spec.requires_attachment!s:<5}{flag}"
            )
