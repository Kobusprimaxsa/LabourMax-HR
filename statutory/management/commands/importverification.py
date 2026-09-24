"""``python manage.py importverification`` — read the ticks back.

Ticks in a spreadsheet that nothing reads are ticks nobody makes twice. This
reads the CHECKS sheet of a completed verification workbook and, for each
reference version whose figures are ALL marked checked with a verifier and a
date, calls the existing ``verifystatutory`` path.

**One ticked row is a CHECK GROUP and records a figure each** (D-258). The
grouping is presentation - the evidence stays per figure, because that is what
somebody will want to interrogate in 2031. A group's membership is recomputed
from the database and its inline summary compared before anything is recorded,
so a workbook cannot assert what it covers.

**Every tick becomes a row in ``reference_figure_check``** (D-256), so the
workbook is disposable and the next export pre-fills what is already done. A
version is verified when EVERY figure in it has a checked record in the
database, not when one workbook happens to hold them all: two people working
two halves on two evenings complete it between them.

**And they may be two different people** (D-270). This used to refuse a version
whose figures carried more than one checker's name — for attribution only, since
``verifystatutory`` recorded one verifier. The workbook is grouped by SOURCE
DOCUMENT and versions cut across documents, so splitting the pass by document
guaranteed that refusal on any version spanning both people's documents. Every
checker is recorded now, the second-pair-of-eyes rule applies to all of them,
and a development identity among them taints the version.

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
from statutory.verification import GOLDEN_COMMAND

try:  # pragma: no cover
    from openpyxl import load_workbook
except ModuleNotFoundError as exc:  # pragma: no cover
    raise CommandError("openpyxl is required: pip install openpyxl") from exc

#: The CHECKS sheet's own layout, named where exportverification names it.
VERSION_COLUMN = 4
VALUE_COLUMN = 7  # the inline summary of every figure the group covers
KEY_COLUMN = 10
CHECKED_COLUMN = 11
BY_COLUMN = 12
DATE_COLUMN = 13
NOTE_COLUMN = 14


def _as_date(value) -> datetime.date | None:
    """A cell's date, or None if it does not hold one.

    Excel hands back a ``datetime`` for a real date cell and a string for one
    somebody typed. Both are ordinary; anything else is not a date, and saying
    so beats recording a check against the wrong day.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _out_of_scope(version, total) -> str:
    """Why this version can never be completed, or "" if it simply has not been.

    Two kinds, and neither is somebody falling behind (D-278):

    **Superseded.** Its fixture has been rewritten under the new label, so no
    row maps back to it and it owns nothing to check — which is what produced
    the "0 of 0 checked" line that read like a broken report and sent somebody
    looking for the missing figures.

    **Expired.** Every row it loaded has closed. The 2023 BCCCI agreement runs
    to 1 April 2026 and no payroll period after that can read a figure from it,
    so verifying it would change nothing about any payslip this system will
    ever produce.
    """
    if hasattr(version, "superseded_by"):
        return (
            f"superseded by {version.superseded_by.version_label} — nothing reads it, "
            f"and its rows now answer to the newer label"
        )
    if version.applies_until and version.applies_until <= datetime.date.today():
        return (
            f"stopped applying {version.applies_until:%d %B %Y} — no payroll period "
            f"can reach a figure in it"
        )
    if total is None or total.figures == 0:
        return "no loaded figures map to this label"
    return ""


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
            help=f"`{GOLDEN_COMMAND}` passed on the commit this is run from: the "
            "published worked examples reproduce on the figures loaded (D-283). Without "
            "it a version is recorded verified but stays invisible to in_force_on().",
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
        if "Checks" not in book.sheetnames:
            raise CommandError(
                f"{path} has no 'Checks' sheet. This reads a workbook produced by "
                f"`manage.py exportverification`. A workbook whose tick sheet is called "
                f"'Figures' predates check groups (D-258) — export a fresh one; every "
                f"tick already imported is in the database and will be pre-filled."
            )

        self._refuse_a_stale_workbook(book, path)

        rows = self._read(book["Checks"])
        if not rows:
            raise CommandError("The Checks sheet holds no rows.")

        by_version: dict[str, list[dict]] = {}
        for row in rows:
            by_version.setdefault(row["version"], []).append(row)

        # PREVIEW IS APPLY, ROLLED BACK (D-145, D-270). A dry run used to skip
        # the writes and then report the DATABASE, so a fully ticked workbook
        # said "0 of N checked" however much was in it - which reads as a
        # failure and sent somebody hunting a bug that was not there. It now
        # does exactly what a real run does and rolls the transaction back, so
        # the report is what the import WOULD record, accurate by construction
        # rather than by a second code path somebody has to keep in step.
        if options["dry_run"]:
            with transaction.atomic():
                outcome = self._process(by_version, current_through, options)
                transaction.set_rollback(True)
        else:
            outcome = self._process(by_version, current_through, options)

        verified, refused, incomplete, already, recorded, out_of_scope = outcome

        # ALWAYS, even at nil. This used to print only `if recorded`, so a run
        # that wrote nothing printed nothing at all and read as success — which
        # is how seventeen marked rows came to be discarded in silence (D-272).
        marked = sum(1 for row in rows if row["checked"])
        verb = "Would record" if options["dry_run"] else "Recorded"
        self.stdout.write(f"{marked} marked row(s) read, {recorded} recorded.")
        if marked and not recorded and not refused:
            self.stdout.write(
                "Nothing was written. Every marked row already holds exactly this "
                "check, so there was nothing to add — re-importing the same workbook "
                "is expected to do nothing."
            )
        if recorded:
            self.stdout.write(f"{verb} {recorded} check(s).\n")

        self._report(
            verified,
            refused,
            incomplete,
            already,
            by_version,
            out_of_scope,
            dry_run=options["dry_run"],
        )

        if refused:
            raise CommandError(
                f"{len(refused)} version(s) refused - none of them was verified. Every "
                f"SOUND tick in the workbook was still recorded, including those on a "
                f"refused version: discarding a hundred good ticks over one bad cell is "
                f"the data loss reference_figure_check exists to stop."
            )

    def _process(self, by_version, current_through, options):
        """Both passes, writing for real. The caller decides whether to commit."""
        verified, refused, incomplete, already, out_of_scope = [], [], [], [], []
        recorded = 0

        # PASS ONE: record every sound tick, whatever its version's fate. A
        # version refused over one bad cell must not cost the other hundred.
        problems_by_version = {}
        for label in sorted(by_version):
            problems, sound = self._sift(by_version[label])
            problems_by_version[label] = problems
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
                # OUT OF SCOPE IS NOT OUTSTANDING (D-278). Three versions in
                # the workbook could never be finished and were listed as
                # though somebody had simply not got to them: two SUPERSEDED,
                # whose fixtures no longer carry their labels so they own no
                # rows and read as "0 of 0 checked", and the 2023 BCCCI
                # agreement, every row of which closed on 1 April 2026 so no
                # payroll period can reach it. A list of things to do that
                # holds things nobody can do is a list people stop reading.
                reason = _out_of_scope(version, total)
                if reason:
                    out_of_scope.append((label, reason))
                    continue
                incomplete.append(
                    (
                        label,
                        (total.outstanding + total.queried) if total else 0,
                        total.figures if total else 0,
                    )
                )
                continue
            # MORE THAN ONE CHECKER IS NOT A PROBLEM (D-270). The workbook is
            # organised by source document because that is how a person
            # verifies, and versions cut across documents, so sharing the pass
            # out by document guarantees a version checked by two people. Two
            # people checking different figures is a stronger result than one
            # checking all of them; the refusal that used to stand here was
            # only ever about which single name verified_by_user would hold.
            users = []
            unknown = [
                email
                for email in checkers
                if not AppUser.objects.filter(email__iexact=email).exists()
            ]
            if unknown:
                refused.append((label, [f"no user with email {name}" for name in unknown]))
                continue
            for email in checkers:
                users.append(AppUser.objects.filter(email__iexact=email).first())

            # The second-pair-of-eyes rule applies to EVERY checker. It used to
            # run against checkers[0] alone, which the one-checker refusal
            # happened to cover; lifting that refusal without this would let a
            # loader verify their own load by being the second name on it.
            offenders = [
                user.email
                for user in users
                if version.loaded_by_user_id and version.loaded_by_user_id == user.pk
            ]
            if offenders:
                refused.append(
                    (
                        label,
                        [
                            f"{', '.join(offenders)} loaded this version and may not "
                            f"verify it. Verification is a second reading by a second "
                            f"pair of eyes."
                        ],
                    )
                )
                continue

            verifier, others = checkers[0], checkers[1:]
            try:
                call_command(
                    "verifystatutory",
                    label,
                    verified_by=verifier,
                    also_checked_by=others,
                    current_through=current_through.isoformat(),
                    golden_tests_passed=options["golden_tests_passed"],
                    verbosity=0,
                )
            except CommandError as refusal:
                refused.append((label, [str(refusal)]))
                continue
            verified.append((label, ", ".join(checkers), len(by_version[label])))

        return verified, refused, incomplete, already, recorded, out_of_scope

    def _refuse_a_stale_workbook(self, book, path):
        """A workbook is a SNAPSHOT of the check groups (D-272).

        Load a fixture after exporting and the groups the file was built from
        are no longer the groups that exist. What the file does not mention
        then stops meaning "nothing to do" and starts meaning "rows this file
        never knew about" — and importing it could report a version complete
        when figures in it had never been looked at.

        Keyed on the group keys alone, so recording ticks does not invalidate a
        workbook: the pass can run over as many evenings as it takes, and only
        a change to the DATA refuses it.
        """
        summary = book["Summary"] if "Summary" in book.sheetnames else None
        stamped = None
        if summary is not None:
            for index in range(1, summary.max_row + 1):
                if summary.cell(row=index, column=1).value == verification.FINGERPRINT_LABEL:
                    stamped = (summary.cell(row=index, column=2).value or "").strip()
                    break

        current = verification.corpus_fingerprint()
        if stamped == current:
            return

        raise CommandError(
            f"{path} was exported from different reference data and is STALE "
            f"(workbook {stamped or 'unstamped'}, database {current}). Rows have been "
            f"loaded, superseded or re-encoded since, so this file does not know about "
            f"all of them - and a version could be reported complete with figures in it "
            f"nobody has seen. Export a fresh workbook: every tick already imported is "
            f"in the database and is pre-filled, so nothing checked is asked for twice. "
            f"If this file holds ticks that were never imported, they are the work at "
            f"risk - copy them across by hand before re-exporting."
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
        one, and a query nobody wrote down is a question asked twice. A group
        whose summary has drifted from the database blocks the version and is
        NOT recorded: it was not checked against what is loaded, whatever the
        cell says.
        """
        problems, sound = [], []

        for row in rows:
            state = verification.recorded_outcome(row["checked"])
            if state is None:
                # Blank, or an N from a workbook exported before D-282
                # dropped it from the dropdown: not yet done either way.
                continue

            group = verification.group_by_key(row["key"])
            if group is None:
                problems.append(
                    f"row {row['line']}: '{row['key']}' is not a check group in this "
                    f"database. The reference data has moved since this workbook was "
                    f"exported — export a fresh one, which will pre-fill everything "
                    f"already imported."
                )
                continue

            if row["value"] != group.summary:
                problems.append(
                    f"row {row['line']} {row['key']}: the workbook says\n"
                    f"        '{row['value']}'\n"
                    f"      and the loaded figures are\n"
                    f"        '{group.summary}'\n"
                    f"      This command NEVER writes a figure. If the workbook is right "
                    f"the gazette was transcribed wrongly and that is a new load, not a "
                    f"tick; if the loaded values are right, restore the cell."
                )
                continue

            if not row["by"]:
                problems.append(f"row {row['line']} is ticked with no 'checked by'")
                continue
            if not row["when"]:
                problems.append(f"row {row['line']} is ticked with no date")
                continue

            # THE SILENT ZERO (D-272). These two used to be checked for being
            # NON-BLANK here and then resolved in _record(), which skipped the
            # row with a bare `continue` when the lookup failed - under a
            # comment claiming the row was "named in _sift's problems", which it
            # never was. Seventeen marked rows, nothing written, nothing said.
            # Both are now resolved HERE, where a failure becomes a problem that
            # refuses the version and names the cell.
            if AppUser.objects.filter(email__iexact=row["by"]).first() is None:
                problems.append(
                    f"row {row['line']} is ticked by '{row['by']}', which is not a user "
                    f"of this system. A check is evidence and has to point at somebody: "
                    f"create the account, or correct the address to the one they use."
                )
                continue
            if _as_date(row["when"]) is None:
                problems.append(
                    f"row {row['line']} has '{row['when']}' in the date column, which is "
                    f"not a date. Use YYYY-MM-DD, or let Excel write a real date."
                )
                continue

            if state is ReferenceFigureCheck.Outcome.QUERIED:
                note = row["note"] or ""
                problems.append(
                    f"row {row['line']} QUERY on {row['key']} ({group.label}): "
                    + (note or "(no note — the QUERY does not say what is wrong)")
                )
                if not note:
                    continue  # the CHECK would refuse it anyway, and rightly.

            # One ticked group, one record per figure it covers.
            for figure in group.figures:
                sound.append(
                    {**row, "outcome": state, "row_key": figure.key, "loaded": figure.value}
                )

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
                when = _as_date(row["when"])
                # _sift has already refused a row whose checker or date will not
                # resolve, so reaching here with either missing is a BUG in the
                # sifting rather than bad input - and the one thing this loop
                # must never do again is pass over a marked row in silence.
                if user is None or when is None:  # pragma: no cover - guarded by _sift
                    raise CommandError(
                        f"{row['row_key']} passed sifting with checker {row['by']!r} and "
                        f"date {row['when']!r}, one of which does not resolve. This is a "
                        f"defect in _sift(), not in the workbook - nothing was written."
                    )
                current = existing.get(row["row_key"])
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
                    row_key=row["row_key"],
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

    def _report(self, verified, refused, incomplete, already, by_version, out_of_scope, *, dry_run):
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written.\n"))

        if verified:
            word = "Would verify" if dry_run else "Verified"
            self.stdout.write(self.style.SUCCESS(f"{word} {len(verified)} version(s):"))
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
        if out_of_scope:
            self.stdout.write(f"\nOut of scope — not checked ({len(out_of_scope)}):")
            for label, reason in out_of_scope:
                self.stdout.write(f"  {label} — {reason}")
        if refused:
            self.stdout.write(self.style.ERROR(f"\nREFUSED ({len(refused)}):"))
            for label, problems in refused:
                self.stdout.write(self.style.ERROR(f"  {label}"))
                for problem in problems:
                    self.stdout.write(f"      {problem}")
