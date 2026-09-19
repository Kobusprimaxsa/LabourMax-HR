"""``python manage.py importverification`` — read the ticks back.

Ticks in a spreadsheet that nothing reads are ticks nobody makes twice. This
reads a completed verification workbook and, for each reference version whose
figures are ALL marked checked with a verifier and a date, calls the existing
``verifystatutory`` path.

**It writes the verification record and NOTHING else** (D-251). If a value in
the workbook differs from the loaded value, that is a QUERY for a human and this
command refuses that version and names the row. Someone editing a rate in Excel
and having it flow into reference data is the failure the whole loader
architecture exists to prevent, and a convenient importer is exactly how it
would happen.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from core.models import AppUser
from statutory import verification
from statutory.models import ReferenceDataVersion

try:  # pragma: no cover
    from openpyxl import load_workbook
except ModuleNotFoundError as exc:  # pragma: no cover
    raise CommandError("openpyxl is required: pip install openpyxl") from exc

VERSION_COLUMN = 4
VALUE_COLUMN = 7
KEY_COLUMN = 10
CHECKED_COLUMN = 11
BY_COLUMN = 12
DATE_COLUMN = 13
NOTE_COLUMN = 14


class Command(BaseCommand):
    help = "Verify reference versions from a completed verification workbook."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The completed workbook.")
        parser.add_argument(
            "--current-through",
            required=True,
            help="YYYY-MM-DD. How far the verifier vouches for every version this run "
            "records. Run again with a different date for versions that differ.",
        )
        parser.add_argument(
            "--golden-tests-passed",
            action="store_true",
            help="The published worked examples reproduce. Without it a version is "
            "recorded verified but stays invisible to in_force_on().",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be verified and write nothing.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"{path} does not exist.")
        try:
            current_through = datetime.date.fromisoformat(options["current_through"])
        except ValueError as exc:
            raise CommandError("--current-through must be YYYY-MM-DD.") from exc

        book = load_workbook(path, data_only=True)
        if "Figures" not in book.sheetnames:
            raise CommandError(
                f"{path} has no 'Figures' sheet. This reads a workbook produced by "
                f"`manage.py exportverification`."
            )

        rows = self._read(book["Figures"])
        if not rows:
            raise CommandError("The Figures sheet holds no rows.")

        by_version: dict[str, list[dict]] = {}
        for row in rows:
            by_version.setdefault(row["version"], []).append(row)

        verified, refused, incomplete, already = [], [], [], []

        for label in sorted(by_version):
            version = ReferenceDataVersion.objects.filter(version_label=label).first()
            if version is None:
                refused.append((label, ["no reference version is loaded under this label"]))
                continue
            if version.verified_at and version.golden_tests_passed:
                already.append(label)
                continue

            problems = self._problems(by_version[label])
            if problems:
                refused.append((label, problems))
                continue

            outstanding = [r for r in by_version[label] if r["checked"] != "Y"]
            if outstanding:
                incomplete.append((label, len(outstanding), len(by_version[label])))
                continue

            verifier = self._one_verifier(by_version[label])
            if isinstance(verifier, list):
                refused.append((label, verifier))
                continue

            user = AppUser.objects.filter(email__iexact=verifier).first()
            if user is None:
                refused.append((label, [f"no user with email {verifier}"]))
                continue
            if version.loaded_by_user_id and version.loaded_by_user_id == user.pk:
                refused.append(
                    (
                        label,
                        [
                            f"{verifier} loaded this version and may not verify it. "
                            f"Verification is a second reading by a second pair of eyes."
                        ],
                    )
                )
                continue

            if not options["dry_run"]:
                call_command(
                    "verifystatutory",
                    label,
                    verified_by=verifier,
                    current_through=current_through.isoformat(),
                    golden_tests_passed=options["golden_tests_passed"],
                    verbosity=0,
                )
            verified.append((label, verifier, len(by_version[label])))

        self._report(verified, refused, incomplete, already, by_version, dry_run=options["dry_run"])

        if refused:
            raise CommandError(
                f"{len(refused)} version(s) refused. Nothing about them was recorded; "
                f"every other version in the workbook was processed."
            )

    # ------------------------------------------------------------------ reading

    def _read(self, sheet) -> list[dict]:
        rows = []
        for index in range(2, sheet.max_row + 1):
            key = sheet.cell(row=index, column=KEY_COLUMN).value
            if not key:
                continue
            checked = (sheet.cell(row=index, column=CHECKED_COLUMN).value or "").strip().upper()
            by = sheet.cell(row=index, column=BY_COLUMN).value or ""
            when = sheet.cell(row=index, column=DATE_COLUMN).value
            rows.append(
                {
                    "line": index,
                    "key": str(key).strip(),
                    "version": str(
                        sheet.cell(row=index, column=VERSION_COLUMN).value or ""
                    ).strip(),
                    "value": self._text(sheet.cell(row=index, column=VALUE_COLUMN).value),
                    "checked": checked,
                    "by": str(by).strip(),
                    "when": when,
                    "note": str(sheet.cell(row=index, column=NOTE_COLUMN).value or "").strip(),
                }
            )
        return rows

    @staticmethod
    def _text(value) -> str:
        """Excel hands back a float for a cell that looks numeric; the workbook
        writes every value as text, so anything else means the cell was retyped."""
        if value is None:
            return ""
        return str(value).strip()

    # ------------------------------------------------------------------ checking

    def _problems(self, rows: list[dict]) -> list[str]:
        """Everything that makes a version unverifiable, named row by row."""
        problems = []

        queried = [r for r in rows if r["checked"] == "QUERY"]
        for row in queried:
            note = row["note"] or "(no note — the QUERY does not say what is wrong)"
            problems.append(f"row {row['line']} QUERY on {row['key']}: {note}")

        for row in rows:
            try:
                loaded = verification.current_value(row["key"])
            except ValueError as exc:
                problems.append(f"row {row['line']}: {exc}")
                continue
            if row["value"] != loaded:
                problems.append(
                    f"row {row['line']} {row['key']}: the workbook says "
                    f"'{row['value']}' and the loaded value is '{loaded}'. This command "
                    f"NEVER writes a figure. If the workbook is right the gazette was "
                    f"transcribed wrongly and that is a new load, not a tick; if the "
                    f"loaded value is right, restore the cell."
                )

        checked = [r for r in rows if r["checked"] == "Y"]
        for row in checked:
            if not row["by"]:
                problems.append(f"row {row['line']} is checked with no 'checked by'")
            if not row["when"]:
                problems.append(f"row {row['line']} is checked with no date")
        return problems

    @staticmethod
    def _one_verifier(rows: list[dict]):
        names = {r["by"] for r in rows if r["by"]}
        if len(names) > 1:
            return [
                "more than one person is recorded as the verifier of this version: "
                + ", ".join(sorted(names))
                + ". verifystatutory records one, so split the version or agree who signs."
            ]
        return next(iter(names))

    # ------------------------------------------------------------------ report

    def _report(self, verified, refused, incomplete, already, by_version, *, dry_run):
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written.\n"))

        if verified:
            self.stdout.write(self.style.SUCCESS(f"Verified {len(verified)} version(s):"))
            for label, verifier, count in verified:
                self.stdout.write(f"  {label} — {count} figures, by {verifier}")
        if already:
            self.stdout.write(f"\nAlready recorded, left alone ({len(already)}):")
            for label in already:
                self.stdout.write(f"  {label}")
        if incomplete:
            self.stdout.write(f"\nStill outstanding ({len(incomplete)}):")
            for label, outstanding, total in incomplete:
                self.stdout.write(f"  {label} — {total - outstanding} of {total} checked")
        if refused:
            self.stdout.write(self.style.ERROR(f"\nREFUSED ({len(refused)}):"))
            for label, problems in refused:
                self.stdout.write(self.style.ERROR(f"  {label}"))
                for problem in problems:
                    self.stdout.write(f"      {problem}")
