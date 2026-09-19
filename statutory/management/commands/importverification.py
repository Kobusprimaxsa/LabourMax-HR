"""``python manage.py importverification`` — read the ticks back.

Ticks in a spreadsheet that nothing reads are ticks nobody makes twice. This
reads a completed verification workbook and, for each reference version whose
figures are ALL marked checked with a verifier and a date, calls the existing
``verifystatutory`` path.

**Every tick becomes a row in ``reference_figure_check``** (D-256), so the
workbook is disposable and the next export pre-fills what is already done. A
version is verified when EVERY figure in it has a checked record in the
database, not when one workbook happens to hold them all: two people working
two halves on two evenings complete it between them.

**It writes verification records and NOTHING else** (D-251). If a value in the
workbook differs from the loaded value, that is a QUERY for a human — this
command refuses that version, names the row, and does not record that row as
checked. Someone editing a rate in Excel and having it flow into reference data
is the failure the whole loader architecture exists to prevent, and a convenient
importer is exactly how it would happen. The OTHER rows of a refused version are
still recorded, because discarding a hundred good ticks over one bad cell is the
data loss this table exists to stop.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import AppUser
from statutory import verification
from statutory.models import ReferenceDataVersion, ReferenceFigureCheck

try:  # pragma: no cover
    from openpyxl import load_workbook
except ModuleNotFoundError as exc:  # pragma: no cover
    raise CommandError("openpyxl is required: pip install openpyxl") from exc

#: The workbook's vocabulary for an outcome, and the inverse of
#: ``exportverification.CHECKED_TEXT``. Anything else in the cell is "not yet".
IMPORTED_AS = {
    "Y": ReferenceFigureCheck.Outcome.CHECKED,
    "QUERY": ReferenceFigureCheck.Outcome.QUERIED,
}

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
        recorded = 0

        # PASS ONE: record every sound tick, whatever its version's fate. A
        # version refused over one bad cell must not cost the other hundred.
        problems_by_version = {}
        for label in sorted(by_version):
            problems, sound = self._sift(by_version[label])
            problems_by_version[label] = problems
            if not options["dry_run"]:
                recorded += self._record(label, sound)

        # PASS TWO: a version verifies when the DATABASE says every figure in it
        # is checked - not when one workbook happens to hold them all.
        complete = verification.fully_checked_versions()

        for label in sorted(by_version):
            version = ReferenceDataVersion.objects.filter(version_label=label).first()
            if version is None:
                refused.append((label, ["no reference version is loaded under this label"]))
                continue
            if version.verified_at and version.golden_tests_passed:
                already.append(label)
                continue
            if problems_by_version[label]:
                refused.append((label, problems_by_version[label]))
                continue

            checkers = complete.get(label)
            if checkers is None:
                total = verification.progress_by_version(
                    verification.lines(), verification.latest_checks()
                ).get(label)
                incomplete.append(
                    (
                        label,
                        (total.outstanding + total.queried) if total else 0,
                        total.figures if total else 0,
                    )
                )
                continue
            if len(checkers) > 1:
                refused.append(
                    (
                        label,
                        [
                            "more than one person has checked figures in this version: "
                            + ", ".join(checkers)
                            + ". verifystatutory records one verifier, so agree who signs."
                        ],
                    )
                )
                continue

            verifier = checkers[0]
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

        if recorded:
            self.stdout.write(f"Recorded {recorded} check(s).\n")

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

    def _sift(self, rows: list[dict]) -> tuple[list[str], list[dict]]:
        """Split a version's rows into what blocks it and what can be recorded.

        A QUERY blocks the version AND is recorded — that is the whole value of
        one, and a query nobody wrote down is a question asked twice. A row
        whose value has drifted from the database blocks the version and is NOT
        recorded: it was not checked against what is loaded, whatever the cell
        says.
        """
        problems, sound = [], []

        for row in rows:
            state = IMPORTED_AS.get(row["checked"])
            if state is None:
                continue  # blank, N, or anything else: not yet done.

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
                continue

            if not row["by"]:
                problems.append(f"row {row['line']} is ticked with no 'checked by'")
                continue
            if not row["when"]:
                problems.append(f"row {row['line']} is ticked with no date")
                continue

            if state is ReferenceFigureCheck.Outcome.QUERIED:
                note = row["note"] or ""
                problems.append(
                    f"row {row['line']} QUERY on {row['key']}: "
                    + (note or "(no note — the QUERY does not say what is wrong)")
                )
                if not note:
                    continue  # the CHECK would refuse it anyway, and rightly.

            sound.append({**row, "outcome": state, "loaded": loaded})

        return problems, sound

    def _record(self, label: str, rows: list[dict]) -> int:
        """Write the checks, skipping any that would repeat what is already there.

        Append-only, so a genuine change of mind inserts a second row and the
        first one stands. Re-importing the same workbook on Thursday must not do
        that, though, or the history fills with rows saying the same thing —
        hence the comparison against the latest record.
        """
        existing = verification.latest_checks()
        written = 0
        with transaction.atomic():
            for row in rows:
                user = AppUser.objects.filter(email__iexact=row["by"]).first()
                if user is None:
                    continue  # named in _sift's problems; nothing to record against.
                when = row["when"]
                when = when.date() if isinstance(when, datetime.datetime) else when
                if isinstance(when, str):
                    try:
                        when = datetime.date.fromisoformat(when.strip()[:10])
                    except ValueError:
                        continue
                current = existing.get(row["key"])
                same = (
                    current is not None
                    and current.outcome == row["outcome"]
                    and current.checked_by_id == user.pk
                    and current.checked_on == when
                    and current.note == row["note"]
                )
                if same:
                    continue
                ReferenceFigureCheck.objects.create(
                    row_key=row["key"],
                    version_label=label,
                    outcome=row["outcome"],
                    value_at_check=row["loaded"][:400],
                    checked_by=user,
                    checked_on=when,
                    note=row["note"],
                )
                written += 1
        return written

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
