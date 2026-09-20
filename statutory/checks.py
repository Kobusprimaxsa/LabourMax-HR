"""Consistency checks over loaded reference data — arithmetic the source can settle.

A transcription error in a rate table is invisible. Nothing errors, the payslip
looks plausible, and the figure is wrong by one digit in one band. Proofreading
catches some of it; arithmetic catches more, because published statutory tables are
internally redundant in ways a typo breaks.

The PAYE table is the clearest example. SARS prints each band as "R44 118 + 26% of
taxable income above R245 100", and that R44 118 is *derivable* from the bands below
it: 18% of 245 100. Every base in the table is the running total of the bands beneath
it, so one mistyped digit anywhere makes the whole ladder stop reconciling. The same
holds for the rebates: the tax threshold for each age is exactly the income at which
the cumulative rebates cancel the tax, so 99 000 x 18% must equal the primary rebate
of 17 820 to the cent.

Neither check knows what the correct figures *are*. They only know the figures must
agree with each other, which is enough to catch the error a human proofreader misses
and cheap enough to run on every verification.

These are also the hook for P7's golden tests: this module fails a version on
arithmetic, and the golden tests will fail it on published worked examples.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from statutory.models import (
    Bank,
    MinimumWageRate,
    PayeRebate,
    ReferenceDataVersion,
    SarsSourceCode,
    StatutoryParameter,
    TaxYear,
    TerminationRuleSet,
    WorkingTimeRuleSet,
)

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Issue:
    """One thing that does not add up. ``blocking`` decides whether it stops a load."""

    blocking: bool
    where: str
    message: str

    def __str__(self):
        marker = "REFUSED" if self.blocking else "warning"
        return f"[{marker}] {self.where}: {self.message}"


def check_paye_brackets(year: TaxYear) -> list[Issue]:
    """Every band's cumulative base must equal the tax on all the bands below it.

    Also checks that the bands are contiguous and that the top one is open-ended.
    A gap between bands is worse than an overlap: an income that falls in the gap
    resolves to no band at all, and the calculator raises in the middle of a payroll
    run rather than at load time.
    """
    issues = []
    brackets = list(year.brackets.order_by("bracket_order"))

    if not brackets:
        return [Issue(True, year.label, "no PAYE brackets are loaded")]

    if brackets[0].income_from != 0:
        issues.append(
            Issue(
                True,
                f"{year.label} band 1",
                f"starts at {brackets[0].income_from}, not 0. Taxable income below that "
                f"would resolve to no band at all.",
            )
        )
    if brackets[0].base_tax != 0:
        issues.append(
            Issue(True, f"{year.label} band 1", f"carries a base of {brackets[0].base_tax}, not 0")
        )
    if brackets[-1].income_to is not None:
        issues.append(
            Issue(
                True,
                f"{year.label} band {brackets[-1].bracket_order}",
                "is the top band but has an upper bound. The top band must be open-ended.",
            )
        )

    for previous, current in zip(brackets, brackets[1:], strict=False):
        where = f"{year.label} band {current.bracket_order}"

        if previous.income_to is None:
            issues.append(
                Issue(True, where, f"band {previous.bracket_order} below it is open-ended")
            )
            continue

        if current.income_from != previous.income_to:
            issues.append(
                Issue(
                    True,
                    where,
                    f"starts at {current.income_from} but the band below ends at "
                    f"{previous.income_to}. Bands must touch exactly.",
                )
            )
            continue

        width = previous.income_to - previous.income_from
        expected = previous.base_tax + (width * previous.marginal_rate_pct / HUNDRED)
        if current.base_tax != expected:
            issues.append(
                Issue(
                    True,
                    where,
                    f"base is {current.base_tax}, but the bands below it come to "
                    f"{expected}. One of the two figures is mistyped.",
                )
            )

    return issues


def check_paye_rebates(year: TaxYear) -> list[Issue]:
    """A tax threshold is the income at which the cumulative rebates cancel the tax.

    SARS publishes both, and they are two views of one figure: the threshold times
    the lowest marginal rate equals the rebates the taxpayer qualifies for. If the
    two disagree, one of them was typed wrong.
    """
    issues = []
    rebates = list(year.rebates.order_by("min_age"))
    if not rebates:
        return [Issue(True, year.label, "no PAYE rebates are loaded")]

    first_band = year.brackets.order_by("bracket_order").first()
    if first_band is None:
        return [Issue(True, year.label, "rebates cannot be checked without the brackets")]

    lowest_rate = first_band.marginal_rate_pct / HUNDRED
    cumulative = Decimal(0)

    for rebate in rebates:
        cumulative += rebate.annual_amount
        expected_threshold = cumulative / lowest_rate
        difference = abs(expected_threshold - rebate.tax_threshold_annual)
        # Published thresholds are whole rand, so a rounding difference under R1 is
        # SARS rounding rather than a transcription error.
        if difference >= 1:
            issues.append(
                Issue(
                    True,
                    f"{year.label} {rebate.rebate_type} rebate",
                    f"cumulative rebates of {cumulative} at {first_band.marginal_rate_pct}% "
                    f"imply a threshold of {expected_threshold:.2f}, but "
                    f"{rebate.tax_threshold_annual} is loaded.",
                )
            )

    return issues


def check_gazetted_wage_derivations(*, hours_per_week: int = 45) -> list[Issue]:
    """Where a gazette prints both an hourly and a weekly rate, they should agree.

    A **warning**, never blocking. Gazettes round, and where the published figures
    disagree with the arithmetic the published figure is what the employer will be
    shown — that is a settled decision, not a defect. What this catches is the larger
    discrepancy, the kind that means a rate was copied into the wrong row.
    """
    issues = []
    for rate in MinimumWageRate.objects.exclude(weekly_rate_45h=None):
        implied = rate.hourly_rate * hours_per_week
        difference = abs(implied - rate.weekly_rate_45h)
        if difference > 1:
            issues.append(
                Issue(
                    False,
                    str(rate),
                    f"{rate.hourly_rate} an hour over {hours_per_week} hours is {implied}, "
                    f"but the weekly rate loaded is {rate.weekly_rate_45h}. Gazettes round, "
                    f"so check this is rounding and not a misplaced row.",
                )
            )
    return issues


def check_source_codes() -> list[Issue]:
    """Two invariants the four base flags must satisfy, whatever the readings are.

    **A non-taxable code cannot be in the UIF or SDL base.** Both are built on the
    Fourth Schedule's definition of remuneration, which is also what "subject to
    PAYE" means — so a code outside it is outside all three. (COIDA's "earnings" is
    its own definition and is deliberately not covered by this rule.)

    **A total or a deduction is not a pay line**, so nothing is ever calculated from
    it. A base flag set on 4141 or 3699 means somebody has mistaken a column that
    receives a calculation for one that feeds it, and the result is UIF charged on
    the UIF figure.

    Neither check knows which flags are correct. They catch the two shapes of
    mistake that are wrong under any reading.
    """
    issues = []
    for row in SarsSourceCode.objects.all():
        if not row.is_taxable and (row.is_uif_remuneration or row.is_sdl_remuneration):
            issues.append(
                Issue(
                    True,
                    f"source code {row.code}",
                    "is not taxable but sits in the UIF or SDL base. Both are built on "
                    "the Fourth Schedule definition of remuneration, so a code outside "
                    "it is outside them too.",
                )
            )

        is_not_a_pay_line = row.code_group in {
            SarsSourceCode.Group.TOTAL,
            SarsSourceCode.Group.DEDUCTION,
        }
        in_a_base = any(
            (
                row.is_taxable,
                row.is_uif_remuneration,
                row.is_sdl_remuneration,
                row.is_coida_remuneration,
            )
        )
        if is_not_a_pay_line and in_a_base:
            issues.append(
                Issue(
                    True,
                    f"source code {row.code}",
                    f"is a {row.code_group} but carries a base flag. Nothing is "
                    f"calculated FROM a total or a deduction - it is calculated INTO "
                    f"one.",
                )
            )

    return issues


def check_banks() -> list[Issue]:
    """A branch code that is not six digits will be rejected by the EFT file.

    A **warning**, because a corporate or foreign branch may legitimately differ and
    because this is operational rather than statutory data. What it catches is the
    transposition - a five or seven digit code pasted from a web page - which would
    otherwise surface as a returned payment on the 25th.
    """
    issues = []
    for bank in Bank.objects.exclude(universal_branch_code=""):
        code = bank.universal_branch_code.strip()
        if not code.isdigit() or len(code) != 6:
            issues.append(
                Issue(
                    False,
                    bank.name,
                    f"has branch code {bank.universal_branch_code!r}, which is not six "
                    f"digits. Check it before an EFT file is generated against it.",
                )
            )
    return issues


def check_night_allowance_coherence() -> list[Issue]:
    """The type and the value must agree about whether there IS a figure.

    **Why this check exists, and what changed under it.** BCEA s17(2)(a) requires
    night work to be compensated and deliberately sets no amount. That used to be
    recorded as a ZERO with ``night_allowance_type`` of ``by_agreement`` beside it,
    so zero carried two meanings and only the type column told them apart. Kobus
    raised it in the first verification pass and the pair was held to be enough
    (D-113); O-22 later set the deadline for fixing it properly at the start of P7,
    because the moment a calculator multiplies by that zero it pays nothing.

    The value is now NULL when no figure is stated, and two CHECK constraints hold
    the pair together in the database. This check is therefore no longer the only
    thing standing between the ambiguity and an underpayment — but it stays, because
    a CHECK proves the SHAPE and this proves the CONTENT: a ``percentage`` row is
    also wrong at a value of zero, which the constraint permits and no employee
    would ever notice.

    BLOCKING, because it is a contradiction rather than a judgement call.
    """
    issues = []
    for rule_set in WorkingTimeRuleSet.objects.select_related("sector").all():
        sector = rule_set.sector.code if rule_set.sector else "BCEA default"
        names_a_figure = rule_set.night_allowance_type in {"percentage", "fixed_amount"}
        value = rule_set.night_allowance_value
        if names_a_figure and (value is None or value == 0):
            issues.append(
                Issue(
                    True,
                    f"working_time_rule_set {sector}",
                    f"has night_allowance_type {rule_set.night_allowance_type!r}, which "
                    f"promises a figure, and night_allowance_value of {value}. A "
                    f"calculator reading that pays nothing for night work. Either the "
                    f"amount is missing, or the type should say the statute sets none.",
                )
            )
        if not names_a_figure and value is not None:
            issues.append(
                Issue(
                    True,
                    f"working_time_rule_set {sector}",
                    f"has night_allowance_type {rule_set.night_allowance_type!r}, which "
                    f"names no figure, and a night_allowance_value of {value}. One of "
                    f"the two is wrong, and the value is the one a calculator will use. "
                    f"A type that states no figure carries NULL, never a zero (O-22).",
                )
            )
    return issues


def check_notice_bands() -> list[Issue]:
    """A rule set's notice bands must start at zero, touch with no gap or
    overlap, and have exactly one open-ended top band — the same shape
    ``check_paye_brackets`` already proves for PAYE (D-68).

    "Touch with no gap or overlap" is now a statement about inclusivity as
    well as value (D-158, corrected): two bands sharing a boundary value
    touch cleanly only if EXACTLY ONE of "the lower band's
    ``service_to_inclusive``" and "the upper band's
    ``service_from_inclusive``" is true. Both true means the boundary value
    belongs to both bands — an OVERLAP of exactly one instant, but a real
    one, because ``resolve.notice_band()`` returns whichever band it reaches
    first rather than raising on an ambiguous match. Both false means it
    belongs to neither — a GAP of exactly one instant, and a service length
    landing precisely there raises ``StatutoryValueMissingError`` in the
    middle of whatever called ``resolve.notice_band()``.

    Deliberately does NOT convert between units to compare adjacent
    boundary VALUES. A rule set's own bands touch in the SAME unit on both
    sides by construction (``tools/build_notice_band_fixture.py``'s own
    discipline — a band's ``service_to`` and the next band's
    ``service_from`` are always written as the identical (value, unit)
    pair), so a plain equality check is both correct and honest: it does
    not pretend "26 weeks" and "6 months" are the same thing, it insists
    the data never makes it ask.
    """
    issues = []
    for rule_set in TerminationRuleSet.objects.select_related("sector").all():
        scope = rule_set.sector.code if rule_set.sector else "BCEA default"
        where_set = f"termination_rule_set {scope}"
        bands = list(rule_set.notice_bands.order_by("sequence"))

        if not bands:
            issues.append(Issue(True, where_set, "has no notice bands loaded"))
            continue

        if bands[0].service_from_value != 0 or not bands[0].service_from_inclusive:
            issues.append(
                Issue(
                    True,
                    f"{where_set} band {bands[0].sequence}",
                    f"starts at {bands[0].service_from_value} {bands[0].service_from_unit} "
                    f"(inclusive={bands[0].service_from_inclusive}), not zero and inclusive. "
                    f"A service length of zero would resolve to no band at all.",
                )
            )

        open_ended = [band for band in bands if band.service_to_value is None]
        if len(open_ended) != 1:
            issues.append(
                Issue(
                    True,
                    where_set,
                    f"has {len(open_ended)} open-ended band(s), not exactly 1 "
                    f"({', '.join(str(b.sequence) for b in open_ended) or 'none'}).",
                )
            )
        elif open_ended[0] is not bands[-1]:
            issues.append(
                Issue(
                    True,
                    where_set,
                    f"band {open_ended[0].sequence} is open-ended but is not the last "
                    f"band in sequence. The top band must be the open-ended one.",
                )
            )

        for previous, current in zip(bands, bands[1:], strict=False):
            where = f"{where_set} band {current.sequence}"

            if previous.service_to_value is None:
                issues.append(
                    Issue(True, where, f"band {previous.sequence} below it is open-ended")
                )
                continue

            touches = (
                current.service_from_value == previous.service_to_value
                and current.service_from_unit == previous.service_to_unit
            )
            if not touches:
                issues.append(
                    Issue(
                        True,
                        where,
                        f"starts at {current.service_from_value} {current.service_from_unit} "
                        f"but the band below ends at {previous.service_to_value} "
                        f"{previous.service_to_unit}. Bands must touch exactly, in the same "
                        f"unit.",
                    )
                )
                continue

            claimed_by_both = previous.service_to_inclusive and current.service_from_inclusive
            claimed_by_neither = (
                not previous.service_to_inclusive and not current.service_from_inclusive
            )
            if claimed_by_both:
                issues.append(
                    Issue(
                        True,
                        where,
                        f"OVERLAPS band {previous.sequence} at exactly "
                        f"{current.service_from_value} {current.service_from_unit}: both "
                        f"claim it inclusively (band {previous.sequence}.service_to_inclusive "
                        f"and this band's service_from_inclusive are both true).",
                    )
                )
            elif claimed_by_neither:
                issues.append(
                    Issue(
                        True,
                        where,
                        f"leaves a GAP at exactly {current.service_from_value} "
                        f"{current.service_from_unit}: neither band {previous.sequence} nor "
                        f"this one claims it inclusively, so a service length landing "
                        f"exactly there resolves to no band at all.",
                    )
                )

    return issues


def check_citations() -> list[Issue]:
    """Nothing cited to a placeholder.

    The CHECK constraints refuse a blank citation. They cannot refuse a citation
    that says "TBC", and a row that cites nothing real is the one that survives
    verification because it looks filled in.
    """
    placeholders = ("tbc", "tbd", "todo", "unknown", "n/a", "test fixture", "placeholder")
    issues = []
    for model in (MinimumWageRate, PayeRebate, SarsSourceCode):
        for row in model.objects.all():
            reference = (row.source_reference or "").strip().lower()
            if any(reference.startswith(bad) or reference == bad for bad in placeholders):
                issues.append(
                    Issue(True, str(row), f"is cited to a placeholder: {row.source_reference!r}")
                )
    return issues


#: Identifiers that pin down ONE published document. A gazette notice number, a
#: gazette number and a SARS guide code are each unique in their own series, so
#: two strings carrying the same one are almost always the same instrument
#: written down twice.
_STRONG_MARKERS = (
    re.compile(r"\bGN\s*R?\.?\s*(\d{3,5})\b", re.IGNORECASE),
    re.compile(r"\bGG\s*(\d{4,6})\b", re.IGNORECASE),
    re.compile(r"\bGovernment Gazette\s*(\d{4,6})\b", re.IGNORECASE),
    re.compile(r"\b(PAYE-[A-Z]{2}-\d{2}-[A-Z]\d{2})\b"),
    re.compile(r"\bNotice\s+(\d{3,5})\s+of\s+(\d{4})\b", re.IGNORECASE),
)

#: An Act and its year identifies the ACT, not a document made under it. Two
#: determinations under BCEA s6(3) both name the Act and are two documents; so
#: are the Act itself and a notice published under it. So this decides anything
#: only when NEITHER string carries a strong identifier.
_WEAK_MARKERS = (re.compile(r"\bAct\s+(\d{1,3})\s+of\s+(\d{4})\b", re.IGNORECASE),)


def _citation_keys(patterns, reference: str) -> set[str]:
    found = set()
    for pattern in patterns:
        for match in pattern.finditer(reference):
            found.add(f"{pattern.pattern}:{'|'.join(m.upper() for m in match.groups())}")
    return found


def check_citation_spellings() -> list[Issue]:
    """Two source-document strings naming the same instrument, reported by name.

    WARNING, never a refusal: a genuine revision of a guide is two documents and
    a real case, so this cannot decide. What it can do is say "these two look
    like the same thing" before somebody spends an evening on one of them.

    **Why it exists** (D-257, O-33 closed). The BCCCI Main Agreement was cited
    with the council's full name on the wage rows and abbreviated on the notice
    bands, where the long form plus a pinpoint overran ``source_reference``'s
    200 characters; the SARS code guide was cited as "2026 issue" on nineteen
    codes and as "revision 13" on two. The verification workbook groups by
    source document, so each pair read as two documents: a person could verify
    one spelling to completion and the version would still show incomplete,
    with nothing on screen explaining why. That is a trap laid for the one part
    of this build that depends on a human finishing something tedious, and the
    conditions that produced it recur every March.
    """
    from statutory import verification

    strong: dict[str, set[str]] = {}
    weak: dict[str, set[str]] = {}
    for line in verification.lines():
        strong.setdefault(line.document, set()).update(
            _citation_keys(_STRONG_MARKERS, line.document)
        )
        weak.setdefault(line.document, set()).update(_citation_keys(_WEAK_MARKERS, line.document))

    issues = []
    names = sorted(strong)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            shared = strong[first] & strong[second]
            if not shared and not strong[first] and not strong[second]:
                shared = weak[first] & weak[second]
            if not shared:
                continue
            issues.append(
                Issue(
                    False,
                    "source_reference",
                    "two source documents share an identifier and may be the same "
                    "instrument cited twice. The workbook groups them apart, so a "
                    "version reads incomplete with one of them fully checked. If they "
                    "are the same, normalise the citation in the fixture and supersede "
                    "(D-257); if they are genuinely different revisions, make the "
                    "difference visible in both strings."
                    f"\n      (1) {first}"
                    f"\n      (2) {second}",
                )
            )
    return issues


def check_something_is_loaded() -> list[Issue]:
    """An empty database passes every other check in this module. That is the bug.

    Each check here iterates rows and reports what does not add up. Over no rows,
    every one of them returns nothing and ``checkstatutory`` prints "Every check
    reconciles" — a green tick on a database that knows no rates at all. It is
    exactly the shape of the five defects P0 turned up: protection that reads
    convincingly and does nothing, because the thing it protects was not there.

    Called by the reporting command rather than by ``run_all``, because ``run_all``
    gates the verification of a single version and versions are verified in any
    order. The tables listed are the ones a payroll run cannot proceed without.
    """
    empty = [
        name
        for name, model in (
            ("minimum_wage_rate", MinimumWageRate),
            ("tax_year", TaxYear),
            ("statutory_parameter", StatutoryParameter),
        )
        if not model.objects.exists()
    ]
    if not empty:
        return []

    return [
        Issue(
            True,
            "reference data",
            f"nothing is loaded in {', '.join(empty)}. Every other check passes over an "
            f"empty table, so a green result here would mean nothing. Run "
            f"'manage.py loadstatutory reference/ref-2026.03.01.json' and the other "
            f"fixtures in reference/ first.",
        )
    ]


def check_fixture_checksums(directory: str | Path | None = None) -> list[Issue]:
    """Every loaded ``reference_data_version``'s checksum against the fixture
    file that produced it, as that file reads RIGHT NOW — not only at the
    moment somebody happens to attempt a reload.

    ``statutory/loader.py`` fingerprints a fixture and compares it against
    ``reference_data_version.checksum`` — but only inside
    ``load_reference_data()``, as a side effect of trying to load a version
    label that already exists. Nobody runs that comparison as a standing
    fact about the database; it only fires if a load is attempted. A fixture
    edited in place after loading — exactly what happened to
    ``ref-2026.03.01-rules.json`` and ``ref-2026.03.01-sd1.json`` in the
    commit that closed D-68 — therefore drifts from what the database holds,
    silently, for as long as nobody tries to reload it. This check makes
    that comparison a standing one: it reads every fixture on disk, and for
    any whose ``version_label`` has already been loaded, recomputes the
    fingerprint and compares it to what is stored.

    Matches fixtures to versions by the ``version_label`` INSIDE each file,
    the same field ``load_reference_data`` itself keys on — never by
    filename, which is a convention nothing enforces.
    """
    from statutory.loader import FIXTURE_DIRECTORY, fingerprint

    base = Path(directory) if directory else Path(FIXTURE_DIRECTORY)
    issues = []
    for path in sorted(base.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            continue
        label = document.get("version_label")
        tables = document.get("tables")
        if not label or tables is None:
            continue

        version = ReferenceDataVersion.objects.filter(version_label=label).first()
        if version is None or not version.checksum:
            continue

        current = fingerprint(tables)
        if current != version.checksum:
            issues.append(
                Issue(
                    True,
                    f"reference_data_version {label}",
                    f"the fixture at {path.name} no longer matches what was loaded "
                    f"(checksum {current[:12]} vs stored {version.checksum[:12]}). Either "
                    f"the file was edited after loading without a new version label, or "
                    f"the database has not been reloaded since the file changed. "
                    f"Reference data is never edited in place for a VALUE change (D-56, "
                    f"D-57) — issue a new version if the figures changed, or reload this "
                    f"exact file if the database is simply behind a structural fix.",
                )
            )
    return issues


def check_machine_verified_versions() -> list[Issue]:
    """Versions whose verification tick is not a person's (D-262).

    REPORTS, never refuses, and the distinction is the whole design.
    ``verifystatutory`` has to accept a development identity or nothing
    downstream of P2 could be built against reference data at all — every
    calculator, the leave engine and the payroll run all read rows that
    ``in_force_on()`` hides until something has ticked them. So the tick is
    allowed, the run refuses on it (``payroll/validation.py``, the same code
    path as never-verified), and this check is the standing reminder in between
    — the one place that says out loud, on a database anybody can run it
    against, which figures are still waiting for a human.

    A refusal here would be worse than useless: ``run_all()`` gates
    ``verifystatutory`` itself, so a blocking issue would make the FIRST machine
    verification impossible to record and the SECOND one impossible to undo.
    """
    issues = []
    suspect = (
        ReferenceDataVersion.objects.filter(
            verified_at__isnull=False,
            verified_by_user__email__iendswith=ReferenceDataVersion.DEVELOPMENT_VERIFIER_SUFFIX,
        )
        .select_related("verified_by_user")
        .filter(verified_by_user__isnull=False)
        .order_by("applies_from", "version_label")
    )
    # superseded_by is the REVERSE side of a OneToOne, so there is no _id
    # attribute and touching it on an un-superseded row raises rather than
    # returning None. One query, then a set membership.
    superseded_labels = set(
        ReferenceDataVersion.objects.filter(supersedes__isnull=False).values_list(
            "supersedes__version_label", flat=True
        )
    )
    for version in suspect:
        superseded = (
            " (superseded, so nothing reads it)"
            if version.version_label in superseded_labels
            else ""
        )
        issues.append(
            Issue(
                False,
                f"reference_data_version {version.version_label}",
                f"verified by {version.verified_by_user.email}, which is a development "
                f"identity and not a person{superseded}. RFC 2606 reserves .invalid so "
                f"the address could never have reached anybody. A payroll run refuses on "
                f"this exactly as it refuses on a version nobody verified at all, and "
                f"in_force_on() cannot see it. Re-run verifystatutory naming the person "
                f"who actually checked every figure against the source document.",
            )
        )
    return issues


def run_all(year: TaxYear | None = None) -> list[Issue]:
    """Every row-consistency check, over every tax year unless one is named.

    Deliberately does NOT include ``check_something_is_loaded``. This set is used to
    gate verification of one reference version, and versions are verified in any
    order — refusing to verify the bank list because the wage rates are not loaded
    yet would be wrong. Emptiness is the reporting command's concern, not this one's.
    """
    issues = []
    years = [year] if year is not None else list(TaxYear.objects.all())
    for one in years:
        issues.extend(check_paye_brackets(one))
        issues.extend(check_paye_rebates(one))
    issues.extend(check_gazetted_wage_derivations())
    issues.extend(check_source_codes())
    issues.extend(check_banks())
    issues.extend(check_night_allowance_coherence())
    issues.extend(check_notice_bands())
    issues.extend(check_citations())
    issues.extend(check_citation_spellings())
    issues.extend(check_machine_verified_versions())
    return issues
