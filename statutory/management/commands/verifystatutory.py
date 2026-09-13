"""``python manage.py verifystatutory <version>`` — the second pass, by a second person.

Verification is not a formality and it is not the loader's job. Someone other than
the person who loaded the file checks every figure against the source document,
confirms the golden tests reproduce the published worked examples, and states the
date through which the data is confirmed correct. Only then does payroll run against
it: ``ReferenceDataVersion.in_force_on()`` cannot see an unverified version, and the
staleness guard blocks any run past ``data_current_through``.

The command refuses to let the loader verify their own load. That check is here
rather than in the database because the database cannot tell which of two user ids
was holding the gazette, but it can tell they are the same person.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AppUser
from statutory.models import ReferenceDataVersion


class Command(BaseCommand):
    help = "Record verification of a loaded reference data version, by a second person."

    def add_arguments(self, parser):
        parser.add_argument("version", help="version_label, e.g. REF-2026.03.01")
        parser.add_argument(
            "--verified-by", required=True, help="Email address of the person verifying."
        )
        parser.add_argument(
            "--current-through",
            required=True,
            help=(
                "YYYY-MM-DD. The last date the data is confirmed correct for. Payroll "
                "runs ending after this are blocked."
            ),
        )
        parser.add_argument(
            "--golden-tests-passed",
            action="store_true",
            help="The published SARS and DEL worked examples reproduce exactly.",
        )
        parser.add_argument(
            "--allow-self-verification",
            action="store_true",
            help="Override the second-person rule. Recorded in the description.",
        )

    def handle(self, *args, **options):
        version = ReferenceDataVersion.objects.filter(version_label=options["version"]).first()
        if version is None:
            raise CommandError(f"No reference data version '{options['version']}'.")

        verifier = AppUser.objects.filter(email__iexact=options["verified_by"]).first()
        if verifier is None:
            raise CommandError(f"No user with email {options['verified_by']}.")

        if (
            version.loaded_by_user_id
            and version.loaded_by_user_id == verifier.pk
            and not options["allow_self_verification"]
        ):
            raise CommandError(
                "The person who loaded this version cannot verify it. Verification is a "
                "second reading of the source document by a second pair of eyes - that is "
                "the entire reason the column exists separately from loaded_by_user."
            )

        try:
            current_through = datetime.date.fromisoformat(options["current_through"])
        except ValueError as exc:
            raise CommandError("--current-through must be YYYY-MM-DD.") from exc

        if current_through < version.applies_from:
            raise CommandError(
                f"--current-through {current_through} is before this version applies "
                f"({version.applies_from})."
            )

        if not options["golden_tests_passed"]:
            self.stdout.write(
                self.style.WARNING(
                    "Golden tests not confirmed. The version will be recorded as verified "
                    "but will NOT come into force until they pass."
                )
            )

        version.verified_by_user = verifier
        version.verified_at = timezone.now()
        version.golden_tests_passed = options["golden_tests_passed"]
        version.data_current_through = current_through
        if options["allow_self_verification"]:
            version.description = (
                f"{version.description}\n[Self-verified by {verifier.email} with "
                f"--allow-self-verification.]"
            ).strip()
        version.save()

        self.stdout.write(f"{version.version_label} verified by {verifier.email}.")
        self.stdout.write(f"  Data confirmed correct through {current_through:%d %B %Y}.")
        self.stdout.write(
            f"  Golden tests passed: {'yes' if version.golden_tests_passed else 'NO'}"
        )
        self.stdout.write(
            f"  In force: {'yes' if version.is_usable else 'no - payroll cannot use it yet'}"
        )
