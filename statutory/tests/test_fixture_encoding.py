"""Every fixture is UTF-8, and no builder emits one through a shell redirect.

**This happened, twice, and neither time did anything refuse it** (D-264).
``python tools/build_notice_band_fixture.py > reference/ref-2026.03.01-notice-bands.json``
put U+FFFD REPLACEMENT CHARACTER through every non-ASCII character in the file
— the rand sign, the en dashes in the notes — because PowerShell's ``>``
re-encodes in the console code page. The JSON stayed valid. The loader read it.
The corruption arrived in the verification workbook as mojibake a person would
have had to sit and decide about, in a pass whose whole value is that what is
on the screen is what is in the gazette.

So there are two tests, and the second is the one that lasts. The first catches
a corrupt file that is already in the repo; the second stops the mechanism that
corrupts it, by refusing a builder that prints a document for redirection at
all. A builder that writes its own file with ``encoding="utf-8"`` cannot be got
wrong from the calling shell.
"""

from __future__ import annotations

import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
REPLACEMENT = "�"

FIXTURES = sorted((REPO / "reference").glob("*.json"))
BUILDERS = sorted((REPO / "tools").glob("build_*.py"))


def fixture_complaints(raw: bytes) -> list[str]:
    """What is wrong with these bytes as a statutory fixture. Empty is fine."""
    problems = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("byte order mark")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [*problems, "not UTF-8"]
    if REPLACEMENT in text:
        problems.append("replacement character")
    return problems


def builder_complaints(source: str) -> list[str]:
    """What is wrong with a builder's source. Empty is fine."""
    problems = []
    if "print(json.dumps(" in source:
        problems.append("prints a document for redirection")
    if 'encoding="utf-8"' not in source:
        problems.append("writes without naming an encoding")
    return problems


def test_there_are_fixtures_and_builders_to_check():
    """Both suites below are generated from a directory listing, so an empty
    listing would make every one of them pass over nothing."""
    assert len(FIXTURES) >= 20
    assert len(BUILDERS) >= 14


@pytest.mark.statutory
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_a_fixture_is_utf8_and_carries_no_replacement_character(path):
    """U+FFFD is what a decoder writes when it was handed bytes it could not
    read. It cannot be typed by accident and it is never meant: one in a
    statutory fixture is always damage."""
    raw = path.read_bytes()

    assert fixture_complaints(raw) == [], (
        f"{path.name} is damaged, almost certainly by a shell redirect. Rebuild it "
        f"by running its builder, which writes the file itself, never with '>'."
    )
    json.loads(raw.decode("utf-8"))


@pytest.mark.statutory
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_a_fixture_carries_no_byte_order_mark(path):
    """PowerShell's ``Out-File`` and ``>`` both prepend one in some versions. A
    BOM makes ``json.load`` fail with a message about character 0 that names
    nothing, and it changes the loader's checksum."""
    assert "byte order mark" not in fixture_complaints(path.read_bytes()), (
        f"{path.name} starts with a UTF-8 BOM. Same cause, same fix."
    )


@pytest.mark.statutory
@pytest.mark.parametrize("path", BUILDERS, ids=lambda p: p.name)
def test_a_builder_writes_its_own_file_rather_than_printing_one(path):
    """THE ONE THAT LASTS. Printing a document is an invitation to redirect it,
    and the redirect is what corrupts the file. Catching the corrupt bytes
    afterwards only works if somebody notices; refusing the mechanism works
    whether or not anybody is looking."""
    assert builder_complaints(path.read_text(encoding="utf-8")) == [], (
        f"{path.name} does not write its own file in UTF-8. A builder that prints a "
        f"document is one that gets run with '>', and the shell re-encodes the "
        f"output (D-264). Give it an OUTPUT path and "
        f'write_text(..., encoding="utf-8").'
    )


# --------------------------------------------- watching both guards reject


@pytest.mark.statutory
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R32,40 — the rand sign".encode("cp1252"), "not UTF-8"),
        ("R32,40 � the en dash".encode(), "replacement character"),
        (b"\xef\xbb\xbf{}", "byte order mark"),
    ],
)
def test_the_fixture_guard_rejects_damaged_bytes(raw, expected):
    """PROVE EVERY GUARD FAILS. Above, every fixture in the repo is clean — which
    is what a guard reading nothing at all would also report. These are the exact
    three shapes a Windows shell produces."""
    assert expected in fixture_complaints(raw)


@pytest.mark.statutory
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("print(json.dumps(DOCUMENT, indent=2))", "prints a document for redirection"),
        ("OUTPUT.write_text(json.dumps(DOCUMENT))", "writes without naming an encoding"),
    ],
)
def test_the_builder_guard_rejects_the_shapes_that_caused_this(source, expected):
    assert expected in builder_complaints(source)


@pytest.mark.statutory
def test_a_correct_builder_passes_both():
    """Watched NOT firing, or the two above prove only that a list is non-empty."""
    assert (
        builder_complaints('OUTPUT.write_text(json.dumps(DOCUMENT, indent=2), encoding="utf-8")')
        == []
    )
    assert fixture_complaints("R32,40 — the rand sign".encode()) == []
