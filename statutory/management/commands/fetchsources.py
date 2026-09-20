"""``python manage.py fetchsources`` — put the documents next to the workbook.

The verification pass is 156 checks across 18 source documents. Reading a clause
takes thirty seconds; finding the right page of the right PDF takes two minutes.
That ratio is why the pass had not been done, and it is a tooling problem rather
than a compliance one.

This downloads every distinct ``source_url`` in the loaded data into
``reference/sources/``, named so a person can tell them apart at a glance.
``exportverification`` then links each check group at the page its clause is on.

**The PDFs are NOT committed.** ``reference/sources/`` is in ``.gitignore``: they
are tens of megabytes of public documents, and the URL in ``source_reference`` is
the citation — the file is a convenience, and a convenience that can be rebuilt
by running this command.

**A URL that does not resolve is a CITATION DEFECT, reported as one.** A citation
whose reader cannot reach the document is doing half its job, and finding all of
them in one run beats finding one at 11pm in the middle of a check.

**A document with NO url at all is reported too**, and more loudly, because
nothing will ever fetch it and no amount of retrying will help: somebody has to
put a URL on those rows.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from statutory import verification

#: gov.za and sars.gov.za both refuse a request with no browser agent.
USER_AGENT = "Mozilla/5.0 (compatible; LabourMax-HR/1.0; statutory source fetch)"

TIMEOUT_SECONDS = 60


class Command(BaseCommand):
    help = "Download every cited source document into reference/sources/."

    def add_arguments(self, parser):
        parser.add_argument(
            "--directory",
            default=verification.SOURCES_DIRECTORY,
            help="Where to put them. Default reference/sources/.",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Download again even if the file is already there.",
        )

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR) / options["directory"]
        directory.mkdir(parents=True, exist_ok=True)

        wanted, uncited = self._targets()
        names = verification.source_files()

        fetched, skipped, failed = [], [], []
        for url, document in sorted(wanted.items(), key=lambda pair: names[pair[0]]):
            target = directory / names[url]
            if target.exists() and not options["refresh"]:
                skipped.append(target.name)
                continue
            try:
                self._download(url, target)
            except Exception as problem:  # noqa: BLE001 - every failure is reportable
                failed.append((document, url, self._why(problem)))
                continue
            fetched.append(target.name)

        self._report(directory, fetched, skipped, failed, uncited)

    # ------------------------------------------------------------------ the work

    def _targets(self):
        """Every distinct source URL and the document it belongs to, plus every
        document that has no URL at all.

        Keyed on the URL: one document can cite several (D-273), and the BCEA
        cites three.
        """
        wanted, uncited = {}, set()
        with_a_url = set()
        for line in verification.lines():
            if line.source_url:
                wanted.setdefault(line.source_url, line.document)
                with_a_url.add(line.document)
            else:
                uncited.add(line.document)
        return wanted, uncited - with_a_url

    def _download(self, url: str, target: Path) -> None:
        # http(s) ONLY, checked rather than assumed. source_url is loaded data,
        # and a file: or ftp: scheme in it would make this command read the
        # local disk on the strength of a fixture — a small thing here, and
        # exactly the shape of assumption this codebase does not make.
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"{scheme or 'no'} scheme is not fetchable; expected http(s)")

        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            payload = response.read()
        if not payload:
            raise ValueError("the server returned an empty body")
        target.write_bytes(payload)

    @staticmethod
    def _why(problem: Exception) -> str:
        if isinstance(problem, urllib.error.HTTPError):
            return f"HTTP {problem.code} {problem.reason}"
        if isinstance(problem, urllib.error.URLError):
            return f"unreachable: {problem.reason}"
        return f"{type(problem).__name__}: {problem}"

    # ------------------------------------------------------------------ report

    def _report(self, directory, fetched, skipped, failed, uncited):
        total = len(fetched) + len(skipped) + len(failed)
        self.stdout.write(f"{total} cited document(s) with a source URL.")
        if fetched:
            self.stdout.write(self.style.SUCCESS(f"  downloaded {len(fetched)}"))
            for name in fetched:
                self.stdout.write(f"    {name}")
        if skipped:
            self.stdout.write(f"  already present {len(skipped)} (--refresh to fetch again)")

        if failed:
            self.stdout.write(self.style.ERROR(f"\n{len(failed)} source URL(s) did NOT resolve:"))
            for document, url, why in failed:
                self.stdout.write(self.style.ERROR(f"  {document}"))
                self.stdout.write(f"      {url}")
                self.stdout.write(f"      {why}")
            self.stdout.write(
                "  A citation whose reader cannot reach the document is doing half its "
                "job. Correct the URL in the fixture and supersede (prose only, D-199)."
            )

        if uncited:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(uncited)} cited document(s) have NO source URL at all, so "
                    f"nothing can be fetched for them and no retry will help:"
                )
            )
            for document in sorted(uncited):
                self.stdout.write(f"  {document}")

        self.stdout.write(f"\nIn {directory}")
