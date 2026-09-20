"""``python manage.py checkstatutory`` — does the loaded reference data add up?

Runs the arithmetic in ``statutory/checks.py`` against whatever is in the database.
Meant to be run twice: by the loader immediately after a load, and by the verifier
before recording verification — at which point ``verifystatutory`` runs it anyway and
refuses if anything blocking is outstanding.

It proves nothing about whether the figures are the *right* ones. It proves they
agree with each other, which is what catches a mistyped digit.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from statutory import checks
from statutory.models import MinimumWageRate, SarsSourceCode, StatutoryParameter, TaxYear


class Command(BaseCommand):
    help = "Check loaded statutory reference data for internal inconsistencies."

    def add_arguments(self, parser):
        parser.add_argument("--tax-year", help="Limit the PAYE checks to one tax year label.")

    def handle(self, *args, **options):
        year = None
        if options.get("tax_year"):
            year = TaxYear.objects.filter(label=options["tax_year"]).first()
            if year is None:
                raise CommandError(f"No tax year '{options['tax_year']}'.")

        issues = (
            checks.check_something_is_loaded()
            + checks.check_fixture_checksums()
            + checks.run_all(year)
        )
        blocking = [issue for issue in issues if issue.blocking]
        warnings = [issue for issue in issues if not issue.blocking]

        for issue in issues:
            style = self.style.ERROR if issue.blocking else self.style.WARNING
            self.stdout.write(style(str(issue)))

        # A fact about history, deliberately NOT an Issue (D-271): nothing reads
        # a superseded version, so there is nothing to act on, and four standing
        # warnings in with the live findings is how a command comes to be
        # skimmed. Still printed, because a check that went silent could not be
        # told apart from one that had stopped working.
        historical = checks.superseded_machine_verified()
        if historical:
            self.stdout.write(
                f"Note: {len(historical)} superseded version(s) carry a development "
                f"verifier (historical, nothing reads them): {', '.join(historical)}."
            )

        if not issues:
            counted = (
                f"{MinimumWageRate.objects.count()} wage rates, "
                f"{StatutoryParameter.objects.count()} parameters, "
                f"{SarsSourceCode.objects.count()} source codes"
            )
            self.stdout.write(self.style.SUCCESS(f"Every check reconciles over {counted}."))
            return

        self.stdout.write("")
        self.stdout.write(f"{len(blocking)} blocking, {len(warnings)} to look at.")
        if blocking:
            raise CommandError("The reference data does not reconcile. It cannot be verified.")
