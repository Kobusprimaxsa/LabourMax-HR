"""The verification workbook, and the one thing it must never do.

P2's last 582 figures are a human job, and the workbook is the shape that job
has been missing. Two rules are load-bearing and both are tested here by
watching them work and then watching them refuse:

**Grouped by SOURCE DOCUMENT.** A person opens GN R.7083 once and checks every
figure citing it. Grouped by table, the same gazette is opened as many times as
there are tables citing it.

**The import records a verification and can never change a figure** (D-251). If
a value in the spreadsheet differs from the loaded one, the version is REFUSED
and the row is named. A convenient importer that wrote figures back is exactly
how a rate typed into Excel at 23:00 ends up on a payslip.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from openpyxl import load_workbook

from statutory import verification
from statutory.loader import load_reference_data
from statutory.models import ReferenceDataVersion, ReferenceFigureCheck, StatutoryParameter

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"

#: Two shipped fixtures with no foreign keys to anything, so a test database can
#: hold them alone. Real fixtures rather than invented ones, because
#: ``row_versions()`` recovers the version by replaying what is in reference/.
FIXTURES = ("ref-2026.03.01-sick-accrual.json", "ref-2026.03.01-employment.json")
SICK = "REF-2026.03.01-SICK-ACCRUAL"
EMPLOYMENT = "REF-2026.03.01-EMPLOYMENT"


@pytest.fixture
def loader(db):
    return get_user_model().objects.create_user(
        email="loader@example.com", password="x" * 14, first_name="A", last_name="Loader"
    )


@pytest.fixture
def checker(db):
    return get_user_model().objects.create_user(
        email="checker@example.com", password="x" * 14, first_name="A", last_name="Checker"
    )


@pytest.fixture
def loaded(loader):
    for name in FIXTURES:
        load_reference_data(
            json.loads((REFERENCE / name).read_text(encoding="utf-8")), loaded_by=loader
        )
    return loader


@pytest.fixture
def workbook(loaded, tmp_path):
    path = tmp_path / "verification.xlsx"
    call_command("exportverification", str(path), verbosity=0)
    return path


def figures(path):
    return load_workbook(path)["Figures"]


def mark_all(path, *, checked="Y", by="checker@example.com", when="2026-09-20"):
    """Tick the whole workbook the way a person would, and save it."""
    book = load_workbook(path)
    sheet = book["Figures"]
    for row in range(2, sheet.max_row + 1):
        sheet.cell(row=row, column=11, value=checked)
        sheet.cell(row=row, column=12, value=by)
        sheet.cell(row=row, column=13, value=when)
    book.save(path)
    return path


# ------------------------------------------------- the grouping, which is the point


def test_the_workbook_is_grouped_by_source_document_and_not_by_table(loaded, tmp_path):
    """THE design decision (D-251). Every figure citing one document sits
    together, so the gazette is opened once. Grouped by table it would be opened
    once per table that cites it, and the job never gets finished."""
    lines = verification.lines()
    seen_order = [line.document for line in lines]

    assert seen_order == sorted(seen_order), "documents must not interleave"
    for document in set(seen_order):
        first, last = (
            seen_order.index(document),
            len(seen_order) - 1 - seen_order[::-1].index(document),
        )
        assert last - first + 1 == seen_order.count(document), (
            f"{document} is split across the workbook instead of being one block"
        )


@pytest.mark.parametrize(
    ("reference", "document", "clause"),
    [
        (
            "Basic Conditions of Employment Act 75 of 1997, s37(1)(a)",
            "Basic Conditions of Employment Act 75 of 1997",
            "s37(1)(a)",
        ),
        (
            # The FIRST marker wins: the last would invent a document called
            # "... s35, read with Form BCEA1A (Regulation 2)".
            "Basic Conditions of Employment Act 75 of 1997, s35, read with Form BCEA1A "
            "(Regulation 2), Summary of the Act",
            "Basic Conditions of Employment Act 75 of 1997",
            "s35, read with Form BCEA1A (Regulation 2), Summary of the Act",
        ),
        (
            # A marker inside brackets is not a marker, or the document name is
            # left with an unclosed parenthesis.
            "GN 5970, GG 52232, March 2025 (Basic Conditions of Employment Act 75 of 1997, s6(3))",
            "GN 5970, GG 52232, March 2025 (Basic Conditions of Employment Act 75 of 1997, s6(3))",
            "",
        ),
        (
            # SD1's own name contains the word "clauses".
            "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text "
            "(clauses 3, 8-24), clause 23(1)(a)",
            "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text "
            "(clauses 3, 8-24)",
            "clause 23(1)(a)",
        ),
        (
            # All five SD7 pinpoints have to land on one document.
            "Sectoral Determination 7: Domestic Worker Sector, published in Regulation Gazette "
            "No. 7434 (consolidated text), read with Basic Conditions of Employment Act 75 of "
            "1997 s37(1)(c); SD7 clause 24(1)(a)",
            "Sectoral Determination 7: Domestic Worker Sector, published in Regulation Gazette "
            "No. 7434 (consolidated text)",
            "read with Basic Conditions of Employment Act 75 of 1997 s37(1)(c); SD7 clause "
            "24(1)(a)",
        ),
        (
            "GN R.7083, GG 54075, 3 February 2026 (National Minimum Wage Act 9 of 2018)",
            "GN R.7083, GG 54075, 3 February 2026 (National Minimum Wage Act 9 of 2018)",
            "",
        ),
    ],
)
def test_a_citation_splits_into_its_document_and_its_pinpoint(reference, document, clause):
    assert verification.split_citation(reference) == (document, clause)


def test_a_figure_is_rendered_the_way_a_person_reads_it(loaded):
    """R 32,40 and not 32.4000. A verifier comparing against a gazette should
    not have to translate the database's precision in their head."""
    row = StatutoryParameter.objects.first()
    field = StatutoryParameter._meta.get_field("value_numeric")

    row.unit = StatutoryParameter.Unit.ZAR
    row.value_numeric = verification.decimal.Decimal("32.4000")
    assert verification.format_value(row, field) == "R 32,40"

    row.value_numeric = verification.decimal.Decimal("1234567.89")
    assert verification.format_value(row, field) == "R 1 234 567,89"

    row.unit = StatutoryParameter.Unit.PERCENT
    row.value_numeric = verification.decimal.Decimal("1.00")
    assert verification.format_value(row, field) == "1,00 %"


def test_the_workbook_says_it_is_not_a_source_of_truth(workbook):
    summary = load_workbook(workbook)["Summary"]
    text = " ".join(str(c.value) for row in summary.iter_rows() for c in row if c.value)
    assert "NEVER A SOURCE OF TRUTH" in text


def test_a_machine_verified_version_does_not_read_as_done(loaded, tmp_path, checker):
    """The four BCCCI versions were verified by a development identity. They must
    not look finished on the summary sheet or nobody will ever check them."""
    version = ReferenceDataVersion.objects.get(version_label=SICK)
    machine = get_user_model().objects.create_user(
        email="claude-verification@labourmax.invalid", password="x" * 14
    )
    version.verified_at = datetime.datetime(2026, 9, 19, tzinfo=datetime.UTC)
    version.verified_by_user = machine
    version.golden_tests_passed = True
    version.data_current_through = datetime.date(2029, 2, 28)
    version.save()

    path = tmp_path / "machine.xlsx"
    call_command("exportverification", str(path), verbosity=0)
    summary = load_workbook(path)["Summary"]
    text = " ".join(str(c.value) for row in summary.iter_rows() for c in row if c.value)

    assert "MACHINE ONLY" in text
    assert "still needs a human" in text


def test_exporting_over_an_existing_workbook_is_refused(workbook):
    """Overwriting one is throwing away somebody's evening — the ticks live only
    in the file."""
    with pytest.raises(CommandError) as raised:
        call_command("exportverification", str(workbook), verbosity=0)
    assert "already exists" in str(raised.value)


# ------------------------------------------------------------- 2f: the round trip


def test_a_fully_checked_workbook_verifies_its_versions_and_moves_no_figure(workbook, checker):
    """THE ROUND TRIP. Export, tick everything, import: the versions verify and
    every figure is exactly as it was."""
    before = {line.key: line.value for line in verification.lines()}
    assert before, "the fixture loaded nothing"

    mark_all(workbook)
    call_command(
        "importverification",
        str(workbook),
        current_through="2027-02-28",
        golden_tests_passed=True,
        verbosity=0,
    )

    for label in (SICK, EMPLOYMENT):
        version = ReferenceDataVersion.objects.get(version_label=label)
        assert version.verified_at is not None, label
        assert version.verified_by_user.email == "checker@example.com"
        assert version.data_current_through == datetime.date(2027, 2, 28)
        assert version.is_usable

    after = {line.key: line.value for line in verification.lines()}
    assert after == before, "importing a workbook must never move a figure"


def test_a_figure_edited_in_the_workbook_refuses_the_version_and_names_the_row(workbook, checker):
    """The failure this whole architecture exists to prevent, watched refusing.
    Somebody 'corrects' a rate in Excel; the import must not carry it into the
    reference data, and must not quietly verify around it either."""
    mark_all(workbook)
    book = load_workbook(workbook)
    sheet = book["Figures"]
    edited_key = sheet.cell(row=2, column=10).value
    original = sheet.cell(row=2, column=7).value
    sheet.cell(row=2, column=7, value="R 99,99")
    book.save(workbook)

    with pytest.raises(CommandError) as raised:
        call_command(
            "importverification",
            str(workbook),
            current_through="2027-02-28",
            golden_tests_passed=True,
            verbosity=0,
        )

    assert "refused" in str(raised.value).lower()

    edited_version = next(line.version for line in verification.lines() if line.key == edited_key)
    assert ReferenceDataVersion.objects.get(version_label=edited_version).verified_at is None
    assert verification.current_value(edited_key) == original, "the figure must not have moved"


def test_the_refusal_names_the_row_and_both_values(workbook, checker, capsys):
    mark_all(workbook)
    book = load_workbook(workbook)
    sheet = book["Figures"]
    key = sheet.cell(row=2, column=10).value
    sheet.cell(row=2, column=7, value="R 99,99")
    book.save(workbook)

    with pytest.raises(CommandError):
        call_command("importverification", str(workbook), current_through="2027-02-28")

    output = capsys.readouterr().out
    assert key in output
    assert "R 99,99" in output
    assert "NEVER writes a figure" in output


# --------------------------------------------------------------- 2d: QUERY refuses


def test_a_queried_row_refuses_the_version_and_repeats_the_note(workbook, checker):
    mark_all(workbook)
    book = load_workbook(workbook)
    sheet = book["Figures"]
    sheet.cell(row=2, column=11, value="QUERY")
    sheet.cell(row=2, column=14, value="Gazette says 27 days, not 26.")
    book.save(workbook)

    with pytest.raises(CommandError):
        call_command("importverification", str(workbook), current_through="2027-02-28")

    assert ReferenceDataVersion.objects.get(version_label=SICK).verified_at is None


def test_a_query_with_no_note_is_refused_for_saying_nothing(workbook, checker, capsys):
    mark_all(workbook)
    book = load_workbook(workbook)
    book["Figures"].cell(row=2, column=11, value="QUERY")
    book.save(workbook)

    with pytest.raises(CommandError):
        call_command("importverification", str(workbook), current_through="2027-02-28")

    assert "does not say what is wrong" in capsys.readouterr().out


# ------------------------------------------------- 2c and 2e: who, and running twice


def test_the_loader_may_not_verify_their_own_load(workbook, loaded):
    """The second-person rule, reached through the workbook rather than around
    it. verifystatutory refuses this and so must anything that calls it."""
    mark_all(workbook, by="loader@example.com")

    with pytest.raises(CommandError):
        call_command("importverification", str(workbook), current_through="2027-02-28")

    assert ReferenceDataVersion.objects.get(version_label=SICK).verified_at is None


def test_the_export_says_which_versions_a_named_verifier_may_not_sign(loaded, tmp_path):
    """2c: found out before the evening is spent, not after."""
    path = tmp_path / "for-loader.xlsx"
    call_command("exportverification", str(path), verifier="loader@example.com", verbosity=0)
    summary = load_workbook(path)["Summary"]
    text = " ".join(str(c.value) for row in summary.iter_rows() for c in row if c.value)

    assert "loader@example.com may verify?" in text
    assert "NO — you loaded it" in text


def test_a_half_finished_workbook_verifies_nothing_and_says_how_far_it_got(
    workbook, checker, capsys
):
    mark_all(workbook, checked="N")
    book = load_workbook(workbook)
    sheet = book["Figures"]
    for row in range(2, sheet.max_row + 1):
        if sheet.cell(row=row, column=4).value == SICK:
            sheet.cell(row=row, column=11, value="Y")
    book.save(workbook)

    call_command("importverification", str(workbook), current_through="2027-02-28", verbosity=1)

    assert ReferenceDataVersion.objects.get(version_label=SICK).verified_at is not None
    assert ReferenceDataVersion.objects.get(version_label=EMPLOYMENT).verified_at is None
    assert "Still outstanding" in capsys.readouterr().out


def test_running_the_import_twice_leaves_the_first_run_alone(workbook, checker, capsys):
    """2e: four documents on Tuesday, five on Thursday. The second run must not
    complain about Tuesday's work or re-stamp it with Thursday's date."""
    mark_all(workbook)
    call_command(
        "importverification",
        str(workbook),
        current_through="2027-02-28",
        golden_tests_passed=True,
        verbosity=0,
    )
    first = ReferenceDataVersion.objects.get(version_label=SICK).verified_at

    call_command(
        "importverification",
        str(workbook),
        current_through="2028-02-29",
        golden_tests_passed=True,
        verbosity=1,
    )

    version = ReferenceDataVersion.objects.get(version_label=SICK)
    assert version.verified_at == first, "a recorded verification is not re-stamped"
    assert version.data_current_through == datetime.date(2027, 2, 28)
    assert "Already recorded" in capsys.readouterr().out


def test_a_dry_run_writes_nothing(workbook, checker):
    mark_all(workbook)
    call_command(
        "importverification",
        str(workbook),
        current_through="2027-02-28",
        golden_tests_passed=True,
        dry_run=True,
        verbosity=0,
    )
    assert ReferenceDataVersion.objects.get(version_label=SICK).verified_at is None


# ------------------------------ the ticks are evidence, so they live in the database


def mark_keys(path, keys, *, checked="Y", by="checker@example.com", when="2026-09-20", note=""):
    """Tick only the named row keys, leaving the rest blank."""
    book = load_workbook(path)
    sheet = book["Figures"]
    wanted = set(keys)
    for row in range(2, sheet.max_row + 1):
        if sheet.cell(row=row, column=10).value in wanted:
            sheet.cell(row=row, column=11, value=checked)
            sheet.cell(row=row, column=12, value=by)
            sheet.cell(row=row, column=13, value=when)
            if note:
                sheet.cell(row=row, column=14, value=note)
    book.save(path)
    return path


def ticks(path) -> dict[str, str]:
    """What the workbook says is checked, keyed by row key."""
    sheet = load_workbook(path)["Figures"]
    return {
        sheet.cell(row=row, column=10).value: (sheet.cell(row=row, column=11).value or "")
        for row in range(2, sheet.max_row + 1)
    }


def test_a_re_export_after_loading_new_rows_carries_every_earlier_tick_forward(
    workbook, checker, tmp_path
):
    """THE HAZARD THIS CLOSES. Three P2 items are still to load, and each one
    needs a re-export. Before this, the first re-export threw away however many
    evenings had accumulated, because the ticks existed only in the file.

    So: tick half, import, load a reference row that did not exist when the
    workbook was written, re-export, and the earlier ticks must still be there
    with the new figures unchecked beside them.
    """
    keys = sorted(ticks(workbook))
    half = keys[: len(keys) // 2]
    assert half, "the fixture produced too few figures to halve"

    mark_keys(workbook, half)
    call_command("importverification", str(workbook), current_through="2027-02-28", verbosity=0)

    load_reference_data(
        json.loads((REFERENCE / "ref-2026.03.01-remuneration.json").read_text(encoding="utf-8")),
        loaded_by=get_user_model().objects.get(email="loader@example.com"),
    )

    reexported = tmp_path / "round-two.xlsx"
    call_command("exportverification", str(reexported), verbosity=0)
    after = ticks(reexported)

    assert set(after) > set(keys), "the new reference row did not reach the workbook"
    for key in half:
        assert after[key] == "Y", f"{key} lost its tick on re-export"
    for key in set(after) - set(keys):
        assert after[key] == "", f"{key} is new and must arrive unchecked"


@pytest.mark.parametrize("order", ["forward", "backward"])
def test_two_partial_workbooks_accumulate_the_same_way_in_either_order(
    loaded, checker, tmp_path, order
):
    """The one-person constraint becomes a convention rather than a data-loss
    risk: whoever ticks what, in whichever order the files are imported, the
    database ends up the same. Git still cannot merge the binary; nothing is
    lost when it tries.

    Both parameters assert the SAME expected state, which is what makes this a
    test of order-independence rather than two tests of one order each.
    """
    first, second = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    call_command("exportverification", str(first), verbosity=0)
    call_command("exportverification", str(second), verbosity=0)

    keys = sorted(ticks(first))
    assert len(keys) >= 4, "too few figures to split between two people"
    mark_keys(first, keys[::2])
    mark_keys(second, keys[1::2])

    for path in (first, second) if order == "forward" else (second, first):
        call_command("importverification", str(path), current_through="2027-02-28", verbosity=0)

    state = {
        check.row_key: (check.outcome, check.checked_by.email)
        for check in ReferenceFigureCheck.objects.all()
    }

    assert state == {
        key: (ReferenceFigureCheck.Outcome.CHECKED, "checker@example.com") for key in keys
    }, "every figure recorded exactly once, whoever ticked it and in whichever order"


def test_a_check_record_can_never_be_edited_or_deleted(workbook, checker):
    """Invariant 4 on this table, watched refusing. A tick is evidence: a change
    of mind inserts a second row and the first one stands."""
    from django.db import DatabaseError, transaction

    mark_all(workbook)
    call_command("importverification", str(workbook), current_through="2027-02-28", verbosity=0)
    check = ReferenceFigureCheck.objects.first()
    assert check is not None

    with pytest.raises(DatabaseError) as raised, transaction.atomic():
        check.note = "actually, no"
        check.save(update_fields=["note"])
    assert "append-only" in str(raised.value)

    with pytest.raises(DatabaseError) as raised, transaction.atomic():
        ReferenceFigureCheck.objects.filter(pk=check.pk).delete()
    assert "append-only" in str(raised.value)


def test_changing_your_mind_inserts_a_second_row_and_keeps_the_first(workbook, checker):
    """A figure queried on Tuesday and checked on Thursday reads in order,
    rather than being overwritten into a single reassuring Y."""
    key = sorted(ticks(workbook))[0]
    mark_keys(workbook, [key], checked="QUERY", note="Gazette may say 27, not 26.")
    with pytest.raises(CommandError):
        # The QUERY blocks its version, and is recorded all the same - a query
        # nobody wrote down is a question asked twice.
        call_command("importverification", str(workbook), current_through="2027-02-28", verbosity=0)

    mark_keys(workbook, [key], checked="Y", when="2026-09-22", note="Checked again; 26 is right.")
    call_command("importverification", str(workbook), current_through="2027-02-28", verbosity=0)

    history = list(ReferenceFigureCheck.objects.filter(row_key=key).order_by("recorded_at", "id"))
    assert [c.outcome for c in history] == [
        ReferenceFigureCheck.Outcome.QUERIED,
        ReferenceFigureCheck.Outcome.CHECKED,
    ]
    assert verification.latest_checks()[key].outcome == ReferenceFigureCheck.Outcome.CHECKED


def test_the_summary_counts_are_right_before_anyone_opens_the_file(workbook, checker, tmp_path):
    """Counted from the DATABASE. A COUNTIFS would read as zero on a freshly
    written file, because openpyxl writes the formula and only Excel evaluates
    it — and zero is exactly the wrong answer to "how far have I got"."""
    keys = sorted(ticks(workbook))
    mark_keys(workbook, keys[:1])
    call_command("importverification", str(workbook), current_through="2027-02-28", verbosity=0)

    fresh = tmp_path / "fresh.xlsx"
    call_command("exportverification", str(fresh), verbosity=0)

    summary = load_workbook(fresh, data_only=True)["Summary"]
    totals = [row for row in summary.iter_rows(values_only=True) if row and row[0] == "TOTAL"]
    assert totals, "the summary has no total row"
    figures, checked, queried, outstanding = totals[0][1:5]
    assert figures == len(keys)
    assert checked == 1, "the one imported tick must show without Excel recalculating"
    assert queried == 0
    assert outstanding == len(keys) - 1
