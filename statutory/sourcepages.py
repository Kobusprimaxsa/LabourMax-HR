"""Which page of which PDF a cited clause is on (D-273).

The verification pass is 156 checks across 18 documents. The reading is thirty
seconds; finding the right page of the right PDF is the other two minutes, and
that ratio is why the pass had not been started. This closes the gap that can be
closed mechanically.

**A WRONG PAGE IS WORSE THAN NO PAGE.** It sends somebody to a clause that is
not the one cited, and they tick against it — which is precisely the failure the
whole verification pass exists to catch. So every rule here is biased towards
answering NOTHING:

* the clause number must appear at the START of a line, which is where a clause
  number is printed and a cross-reference to one is not;
* a page that reads like a table of contents is skipped, because the number is
  on it and the clause is not;
* an ambiguous answer — the number starting lines on more pages than a clause
  plausibly spans — is no answer.

**This is navigation and nothing else.** Nothing here extracts, quotes or
summarises what a clause SAYS, and the workbook must never carry that: it would
make the pass Claude Code's transcription checked against Claude Code's
transcription, which is the one thing it exists to avoid. A page number is a
pointer to a document the person still has to read.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

#: A page whose text contains one of these is an index rather than the clause.
#: The number is on it, which is exactly why it has to be excluded.
CONTENTS_MARKERS = (
    "arrangement of sections",
    "table of contents",
    "index to",
    "contents",
)


#: A token heading lines on more pages than a clause plausibly spans is not a
#: clause number - it is a numbered list, a column of figures, a page header.
AMBIGUOUS_ABOVE = 3


def clause_token(clause: str) -> str | None:
    """The number to look for, or None if the pinpoint does not name one.

    Takes the FIRST number in a pinpoint that names several ("clauses 8, 9 and
    11" opens at 8), because one page has to be chosen and the earliest is the
    one the others follow.
    """
    text = (clause or "").strip()
    if not text:
        return None

    if re.match(r"(?i)^schedule\b", text):
        found = re.search(r"(?i)^schedule\s+([0-9IVX]+)", text)
        return f"schedule {found.group(1)}" if found else None

    # A section: "s37(1)(a)", "ss 20-27", "s9A".
    section = re.match(r"(?i)^ss?\s*(\d+[A-Z]?)\b", text)
    if section:
        return section.group(1)

    # A clause, dotted or not: "clause 12.1(a)", "clauses 3(2), 8 to 17".
    numbered = re.match(r"(?i)^clauses?\s+(\d+(?:\.\d+)*)", text)
    if numbered:
        return numbered.group(1)

    return None


def _pattern(token: str) -> re.Pattern:
    """The token as it is PRINTED at the head of a clause.

    Anchored to the start of a line — a clause number is printed there and a
    cross-reference to one is not, which is most of what keeps this honest.
    """
    if token.startswith("schedule "):
        return re.compile(rf"(?im)^[ \t]*{re.escape(token)}\b")
    # "37. Notice of termination", never a bare "37". That second half is not
    # fussiness: A LINE HOLDING NOTHING BUT A NUMBER IS A PAGE NUMBER, and
    # without it "35" matched thirty-three of the Act's forty pages, the first
    # hit being whichever page happened to be numbered 35. A page chosen that
    # way is exactly the wrong page this module exists not to give.
    if "." in token:
        # A dotted agreement clause prints as "12.1 Provided that..." with no
        # further dot, so one must not be demanded.
        return re.compile(rf"(?m)^[ \t]*{re.escape(token)}[ \t]+[A-Za-z(]")
    # A plain section number prints WITH its dot — "22. (1) In this Chapter",
    # "7. Imposition of value-added tax". Making the dot optional put the VAT
    # Act's s7 on page 87 of 87, matching a bare "7" in a line of Schedule 2.
    return re.compile(rf"(?m)^[ \t]*{re.escape(token)}\.[ \t]+[A-Za-z(]")


#: A real section page carries one or two headings. A contents page carries a
#: column of them.
HEADINGS_PER_CONTENTS_PAGE = 10

#: The DOT is required. Without it the Public Holidays Act's Schedule 1 — a
#: column of "1 January", "21 March", "27 April" — read as twelve headings and
#: the page was thrown away as a contents listing, taking twenty-one check
#: groups with it. A contents entry is "61. Public hearings"; a date is not.
_ANY_HEADING = re.compile(r"(?m)^[ \t]*\d+[A-Z]?\.[ \t]+[A-Za-z(]")


def _is_contents(text: str) -> bool:
    """Is this page an index rather than the clause itself?

    Named markers alone were not enough: an Act's arrangement of sections runs
    over several pages and only the FIRST carries the heading, so s23 and s43
    were both answered with page 3 — a contents page listing them. Counting
    headings catches the continuation pages too, because what makes a contents
    page recognisable is the column of them.
    """
    if any(marker in text.lower() for marker in CONTENTS_MARKERS):
        return True
    return len(_ANY_HEADING.findall(text)) >= HEADINGS_PER_CONTENTS_PAGE


@functools.lru_cache(maxsize=32)
def page_texts(path: str) -> tuple[str, ...]:
    """Every page's text, once per file per process. Empty if unreadable."""
    try:
        import pypdfium2
    except ModuleNotFoundError:  # pragma: no cover - pypdfium2 is a dependency
        return ()
    document = None
    try:
        document = pypdfium2.PdfDocument(path)
        return tuple(page.get_textpage().get_text_range() for page in document)
    except Exception:  # noqa: BLE001 - an unreadable source is "no page", never a crash
        return ()
    finally:
        if document is not None:
            document.close()


def page_for(clause: str, path: Path | str) -> int | None:
    """The 1-based page the clause is printed on, or None.

    None is a perfectly good answer and the common one. It means the workbook
    shows no link for that row and the person opens the document themselves —
    exactly what they did before, and better than being sent somewhere wrong.
    """
    pages = page_texts(str(path))
    if not pages:
        return None

    # A one-page notice has nowhere else to be. Several of the cited gazettes
    # are a single page — the earnings threshold, the UIF ceiling determination
    # — and those often carry no clause pinpoint either, so this is the only
    # answer available for them and it cannot be wrong.
    if len(pages) == 1:
        return 1

    token = clause_token(clause)
    if token is None:
        return None

    pattern = _pattern(token)
    hits = []
    for number, text in enumerate(pages, start=1):
        if not text or not pattern.search(text):
            continue
        if _is_contents(text):
            continue  # the number is here; the clause is not.
        hits.append(number)

    # The FIRST surviving candidate, and that ordering is a property of how
    # these documents are printed rather than a hope: an Act runs its sections
    # in ascending order and its schedules come after the body, so a second hit
    # on "3." is the amendment schedule saying "Section 10 ... is hereby
    # amended" and the first is the section itself.
    #
    # But a token heading lines all over a document is not a section number at
    # all, and there the ordering argument says nothing — so more than a few
    # candidates is no answer.
    if not hits or len(hits) > AMBIGUOUS_ABOVE:
        return None
    return hits[0]
