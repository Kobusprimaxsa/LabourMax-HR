"""Finding the page a clause is printed on, and refusing to guess (D-273).

**A wrong page is worse than none.** It sends somebody to a clause that is not
the one cited and they tick against it, which is the failure the whole
verification pass exists to catch. So the interesting tests here are the ones
where nothing is answered.

The page texts are supplied directly rather than through a PDF: what is under
test is the reading, and a fabricated PDF would only prove pypdfium2 works.
"""

from __future__ import annotations

import pytest

from statutory import sourcepages, verification

pytestmark = [pytest.mark.statutory]


@pytest.fixture
def pages(monkeypatch):
    """Stand in for a document's page texts."""

    def supply(*texts):
        monkeypatch.setattr(sourcepages, "page_texts", lambda path: tuple(texts))

    return supply


# ------------------------------------------------------------ reading a pinpoint


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("s37(1)(a)", "37"),
        ("ss 20-27", "20"),
        ("ss 9-18 and s9A", "9"),
        ("s22(1)-(2). Commencement of Chapter 6.", "22"),
        ("clause 12.1(a)", "12.1"),
        ("clause 23(1)(a)", "23"),
        ("clauses 8, 9 and 11", "8"),
        ("clause 3, definition of 'monthly wage'", "3"),
        ("Schedule 1", "schedule 1"),
        ("", None),
        ("read with Basic Conditions of Employment Act 75 of 1997 s37(1)(c)", None),
    ],
)
def test_the_first_number_in_the_pinpoint_is_the_one_to_look_for(clause, expected):
    """A pinpoint naming several clauses opens at the earliest, because one
    page has to be chosen and the others follow it."""
    assert sourcepages.clause_token(clause) == expected


# -------------------------------------------- the page number trap, which bit


def test_a_line_holding_only_a_number_is_a_page_number_and_never_a_match(pages):
    """THE ONE THAT MATTERED. Matching a bare "35" at line start found it on
    thirty-three of the Act's forty pages — every page numbered 35 and every
    numeric table — and the first hit was whichever came first. Requiring a
    letter after the number is what makes the answer a heading."""
    pages(
        "34\nsomething about deductions\n",
        "35\na page whose printed number is 35\n",
        "35. An employee's wage is calculated by reference to...\n",
    )

    assert sourcepages.page_for("s35(4)(a)", "any.pdf") == 3


def test_a_contents_page_is_skipped_even_where_it_carries_no_heading(pages):
    """An Act's arrangement of sections runs over several pages and only the
    first says so. Counting headings catches the continuation pages, which is
    what sent s23 and s43 both to page 3 before."""
    contents = "\n".join(f"{n}. A section title here" for n in range(1, 30))
    pages("ARRANGEMENT OF SECTIONS\n" + contents, contents, "23. (1) An employer is not...\n")

    assert sourcepages.page_for("s23(1)", "any.pdf") == 3


def test_an_ambiguous_token_answers_nothing(pages):
    """Found on more pages than a clause plausibly spans, so the token is
    something else. Nothing is a good answer."""
    pages(*[f"7. A heading on page {n}\n" for n in range(9)])

    assert sourcepages.page_for("clause 7", "any.pdf") is None


def test_a_clause_that_is_not_there_answers_nothing(pages):
    pages("1. Definitions\n", "2. Application\n")

    assert sourcepages.page_for("clause 99", "any.pdf") is None


def test_a_pinpoint_with_no_number_answers_nothing(pages):
    pages("12. Study leave and qualifications\n")

    assert sourcepages.page_for("", "any.pdf") is None


def test_a_document_with_no_extractable_text_answers_nothing(pages):
    """Both BCCCI gazettes are like this: 99 000 characters of text in which
    no clause number survives the font encoding. Nothing to search, so nothing
    is claimed."""
    pages("", "")

    assert sourcepages.page_for("clause 12.1(a)", "any.pdf") is None


def test_a_dotted_clause_is_found_at_its_own_heading(pages):
    pages("12. STUDY LEAVE\n", "12.1 Provided satisfactory proof is produced...\n")

    assert sourcepages.page_for("clause 12.1(a)", "any.pdf") == 2


# ------------------------------------------------- one document, several urls


def test_source_files_are_keyed_on_the_url_and_not_the_document():
    """The BCEA cites THREE urls under one document string — the Act twice and
    Form BCEA1A, the Summary of the Act. Keyed on the document, the two rows
    citing the summary would have linked to the Act, and a verifier would have
    hunted for the summary's wording in a document that does not contain it
    (D-273). A wrong document is worse than no link, for the same reason a
    wrong page is."""
    lines = [
        verification.Line(
            document="Basic Conditions of Employment Act 75 of 1997",
            clause="s35",
            source_url="https://example.test/act.pdf",
            version="V",
            table="t",
            description="d",
            value="v",
            effective_from="2026-03-01",
            effective_to="",
            key="k1",
        ),
        verification.Line(
            document="Basic Conditions of Employment Act 75 of 1997",
            clause="Form BCEA1A",
            source_url="https://example.test/summary.pdf",
            version="V",
            table="t",
            description="d",
            value="v",
            effective_from="2026-03-01",
            effective_to="",
            key="k2",
        ),
    ]

    files = verification.source_files(lines)

    assert len(files) == 2, "two urls, two files"
    assert len(set(files.values())) == 2, "and two DISTINCT filenames"
    assert all(name.startswith("Basic-Conditions") for name in files.values()), (
        "both still recognisable at a glance in a directory listing"
    )


def test_one_document_with_one_url_keeps_a_clean_filename():
    """Watched NOT firing: the disambiguating digest appears only where a
    document genuinely has more than one url."""
    lines = [
        verification.Line(
            document="Public Holidays Act 36 of 1994",
            clause="Schedule 1",
            source_url="https://example.test/holidays.pdf",
            version="V",
            table="t",
            description="d",
            value="v",
            effective_from="2026-03-01",
            effective_to="",
            key="k1",
        )
    ]

    (name,) = verification.source_files(lines).values()

    assert name == "Public-Holidays-Act-36-of-1994_Act36-1994.pdf"
