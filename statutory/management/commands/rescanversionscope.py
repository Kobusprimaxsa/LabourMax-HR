"""Fill in ``applies_until`` for versions loaded before it existed (D-278).

The loader derives it now, but eighteen versions were loaded before it did and
they all read as open-ended — which is the safe direction and the wrong answer
for the four 2023 BCCCI ones, whose rows every last one closed on 1 April 2026.

Replays the shipped fixtures, exactly as ``check_fixture_checksums()`` and
``verification.row_versions()`` already do: a version's rows are recovered from
the file that loaded them, because no cited table carries a version foreign key
(D-252). Writes nothing but ``applies_until``, and only where the fixture is
still on disk under that label.

Idempotent. Re-run it after a load if you want to be sure, though the loader
sets it and this should then find nothing to do.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from statutory.loader import FIXTURE_DIRECTORY, scope_of
from statutory.models import ReferenceDataVersion


class Command(BaseCommand):
    help = "Recover applies_until for versions loaded before the loader derived it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        scopes = {}
        for path in sorted(Path(FIXTURE_DIRECTORY).glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                continue
            label = document.get("version_label")
            if label:
                scopes[label] = scope_of(document)

        changed, unchanged, unknown = [], 0, []
        for version in ReferenceDataVersion.objects.order_by("applies_from", "version_label"):
            if version.version_label not in scopes:
                # A SUPERSEDED version's fixture has been rewritten under the
                # new label, so its rows can no longer be recovered from disk.
                # Nothing reads a superseded version, so nothing needs this.
                unknown.append(version.version_label)
                continue
            found = scopes[version.version_label]
            if found == version.applies_until:
                unchanged += 1
                continue
            changed.append((version, found))

        for version, found in changed:
            word = "would stop" if options["dry_run"] else "stops"
            self.stdout.write(
                f"  {version.version_label} {word} applying "
                + (f"{found:%d %B %Y}" if found else "never (open-ended)")
            )
            if not options["dry_run"]:
                version.applies_until = found
                version.save(update_fields=["applies_until", "updated_at"])

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
        self.stdout.write(
            f"{len(changed)} version(s) updated, {unchanged} already right, "
            f"{len(unknown)} not in any shipped fixture."
        )
        if unknown:
            self.stdout.write(
                "  Not in any fixture (superseded, so nothing reads them): " + ", ".join(unknown)
            )
