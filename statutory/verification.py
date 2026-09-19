"""The verification workbook: what a person has to check, arranged so they can.

P2's remaining work is not code. Every structure exists, every figure is loaded
and reconciles, and ``in_force_on()`` still cannot see any of it, because
verification is a second person reading each figure off its source document.
That job has had no shape — a list of eighteen version labels tells nobody what
to open or how far they have got — so it has not been started.

**The one design decision here is the grouping: SOURCE DOCUMENT first, then
clause, then effective date.** A person verifies by opening GN R.7083 once and
checking every figure that cites it. Grouped by table, the same gazette is
opened as many times as there are tables citing it and the job never finishes.
Everything else in this module is in service of that.

**The workbook is a WORK AID and never a source of truth.** The fixtures in
``reference/`` are the build input and ``docs/DECISIONS.md`` is the register;
this file produces a spreadsheet and reads ticks back out of it. Nothing here
writes a figure, a citation or an effective date, and
``importverification`` refuses a whole version rather than accept one edited
value — someone correcting a rate in Excel and having it flow into reference
data is the failure this architecture exists to prevent (D-251).

**Rows do not know which version loaded them** (D-252). No cited table carries a
foreign key to ``reference_data_version``; the association lives only in the
fixture files. So the version column is recovered by replaying each shipped
fixture through the loader's own ``locate_row()`` — the same natural-key match
the loader performs, not a second one written here.
"""

from __future__ import annotations

import dataclasses
import decimal
import json
import re
from pathlib import Path

from django.conf import settings
from django.db import models

from statutory.loader import (
    FIXTURE_DIRECTORY,
    FIXTURE_ORDER,
    TABLES,
    ReferenceDataLoadError,
    locate_row,
)
from statutory.models import ReferenceDataVersion, ReferenceFigureCheck

#: Columns every cited table carries that are not figures anybody verifies.
#: ``source_reference`` and ``source_url`` become the grouping, ``notes`` is the
#: transcription reasoning, and the rest is audit scaffolding.
SCAFFOLDING = frozenset(
    {
        "id",
        "public_uid",
        "created_at",
        "updated_at",
        "created_by_user",
        "updated_by_user",
        "source_reference",
        "source_url",
        "notes",
    }
)

#: Fields that NAME a row rather than state a figure. They appear in the
#: description so the verifier knows which row they are looking at, and they do
#: not get a line of their own — "sector = CONTRACT_CLEANING" is not something
#: to check against a gazette, it is which part of the gazette to be in.
#:
#: Listed by name rather than derived from ``spec.natural_key``, because a
#: natural key mixes the two: ``public_holiday``'s key is (country, DATE) and
#: the date is exactly the figure a person checks against Schedule 1.
LABEL_ONLY = frozenset(
    {
        "sector",
        "sector_area",
        "job_grade",
        "tax_year",
        "termination_rule_set",
        "leave_type",
        "sequence",
        "hours_band",
        "country_code",
        "parameter_code",
        "code",
        "rebate_type",
        "bracket_order",
    }
)

#: Decimal fields that are money, so the workbook renders them the way a person
#: reads a wage — R 32,40, not 32.4000. Named one by one: guessing from a column
#: name would eventually print a multiplier or a percentage with a rand sign.
MONEY_FIELDS = {
    ("minimum_wage_rate", "hourly_rate"),
    ("minimum_wage_rate", "weekly_rate_45h"),
    ("minimum_wage_rate", "monthly_rate_45h"),
    ("minimum_wage_rate", "daily_rate_9h"),
    ("paye_tax_bracket", "income_from"),
    ("paye_tax_bracket", "income_to"),
    ("paye_tax_bracket", "base_tax"),
    ("paye_rebate", "annual_amount"),
    ("paye_rebate", "tax_threshold_annual"),
    ("medical_tax_credit_rate", "main_member_monthly"),
    ("medical_tax_credit_rate", "first_dependant_monthly"),
    ("medical_tax_credit_rate", "additional_dependant_monthly"),
    ("working_time_rule_set", "standby_allowance_per_shift"),
}

#: Decimal fields that are a percentage.
PERCENT_FIELDS = {
    ("paye_tax_bracket", "marginal_rate_pct"),
    ("working_time_rule_set", "accommodation_deduction_max_pct"),
}

_CLAUSE_MARKERS = (
    ", clauses ",
    ", clause ",
    ", sections ",
    ", section ",
    ", order para",
    ", Schedule ",
    ", paragraph ",
    ", Part ",
    ", item ",
    ", read with ",
    ", ss ",
    ", s",
)


def split_citation(reference: str) -> tuple[str, str]:
    """Split a ``source_reference`` into the document and the pinpoint within it.

    Two rules, and the loaded citations need both:

    **The FIRST marker wins, not the last.** "Basic Conditions of Employment Act
    75 of 1997, s35, read with Form BCEA1A (Regulation 2), Summary of the Act"
    is one document pinpointed three ways; splitting at the last marker would
    file it under a document called "... s35, read with Form BCEA1A
    (Regulation 2)" and separate it from every other BCEA row.

    **A marker inside brackets is not a marker.** "GN 5970, GG 52232, March 2025
    (Basic Conditions of Employment Act 75 of 1997, s6(3))" has its only comma-s
    inside the parenthesis, and splitting there would leave the document name
    with an unclosed bracket. Same for Sectoral Determination 1's own
    "(clauses 3, 8-24)".

    A reference with no marker outside brackets is all document and no pinpoint,
    which is right for "GN R.7083, GG 54075, 3 February 2026 (National Minimum
    Wage Act 9 of 2018)".
    """
    depth = 0
    for index, character in enumerate(reference):
        if character in "([":
            depth += 1
            continue
        if character in ")]":
            depth = max(0, depth - 1)
            continue
        if depth or character != ",":
            continue
        for marker in _CLAUSE_MARKERS:
            if not reference.startswith(marker, index):
                continue
            # ", s" must be followed by a digit, or it matches ", supplement".
            if marker == ", s" and not reference[index + 3 : index + 4].isdigit():
                continue
            return reference[:index].strip(), reference[index + 2 :].strip()
    return reference.strip(), ""


def format_value(row, field) -> str:
    """One figure, rendered the way the person holding the gazette reads it.

    South African convention throughout: comma as the decimal separator, a
    non-breaking space as the thousands separator, and trailing zeros kept so
    R 32,40 does not read as R 32,4.

    This is the ONLY formatter. ``importverification`` regenerates the same
    string and compares, so a value the person edited in the spreadsheet is
    detected by the comparison rather than by trusting them not to.
    """
    value = getattr(row, field.attname)
    if value is None:
        return ""

    table = row._meta.db_table

    if isinstance(field, models.BooleanField):
        return "Yes" if value else "No"

    if isinstance(field, models.DateField) and not isinstance(field, models.DateTimeField):
        return f"{value:%d %B %Y}"

    if isinstance(value, decimal.Decimal):
        if (table, field.name) in MONEY_FIELDS:
            return "R " + _decimal_text(value, quantum="0.01")
        if (table, field.name) in PERCENT_FIELDS:
            return _decimal_text(value, quantum="0.01") + " %"
        if table == "statutory_parameter":
            unit = getattr(row, "unit", "")
            if unit == "ZAR":
                return "R " + _decimal_text(value, quantum="0.01")
            if unit == "percent":
                return _decimal_text(value, quantum="0.01") + " %"
        return _decimal_text(value)

    if field.choices:
        return str(dict(field.flatchoices).get(value, value))

    return str(value)


def _decimal_text(value: decimal.Decimal, *, quantum: str = "") -> str:
    """A Decimal in South African notation, without inventing or losing precision.

    ``quantum`` is the step to round to, as a string, so money prints two places
    and everything else keeps whatever the gazette stated. Written as the step
    rather than a digit count because a digit count needs ``Decimal(10) ** -n``,
    and a bare ``Decimal(10)`` in this codebase is a statutory figure with no
    citation until ``test_no_hardcoded_rates`` is told otherwise.
    """
    if quantum:
        value = value.quantize(decimal.Decimal(quantum), rounding=decimal.ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    digits, _, fraction = f"{abs(value)}".partition(".")
    grouped = f"{int(digits):,}".replace(",", " ")
    return f"{sign}{grouped},{fraction}" if fraction else f"{sign}{grouped}"


def _scope_of(row) -> str:
    """The sector and area a row is scoped to, or "" where it is not scoped."""
    parts = []
    for name in ("sector", "sector_area"):
        try:
            value = getattr(row, name, None)
        except Exception:  # a scope column the model does not have
            value = None
        if value is not None:
            parts.append(getattr(value, "code", str(value)))
    return " / ".join(parts) or "all sectors"


#: Tables whose generic description would not say WHICH row this is. The
#: fallback below reads the LABEL_ONLY columns, which is right for a wage rate
#: scoped to a sector and useless for a notice band, whose identity is the
#: service range it covers.
IDENTIFY = {
    "termination_notice_band": lambda row: (
        f"Notice band {row.sequence} — {_scope_of(row.termination_rule_set)}, "
        f"{row.service_from_value:g} {row.service_from_unit} to "
        + (
            f"{row.service_to_value:g} {row.service_to_unit}"
            if row.service_to_value is not None
            else "open-ended"
        )
    ),
    "public_holiday": lambda row: f"Public holiday — {row.name}, {row.holiday_date:%d %B %Y}",
    "sars_source_code": lambda row: f"SARS source code {row.code} — {row.description}",
    "paye_tax_bracket": lambda row: f"PAYE bracket {row.bracket_order}, {row.tax_year.label}",
    "paye_rebate": lambda row: f"PAYE {row.rebate_type} rebate, {row.tax_year.label}",
    "medical_tax_credit_rate": lambda row: f"Medical scheme tax credit, {row.tax_year.label}",
    "statutory_parameter": lambda row: (
        f"Statutory parameter {row.parameter_code} ({_scope_of(row)})"
    ),
    "leave_rule_set": lambda row: f"Leave rules — {_scope_of(row)}",
    "working_time_rule_set": lambda row: f"Working time rules — {_scope_of(row)}",
    "termination_rule_set": lambda row: f"Termination rules — {_scope_of(row)}",
    "minimum_wage_rate": lambda row: (
        f"Minimum wage — {_scope_of(row)}"
        + (f", {row.job_grade.code}" if row.job_grade_id else "")
        + f", {row.hours_band} hours"
    ),
}


def describe(row) -> str:
    """What this row is, in words, for someone who does not know the schema."""
    table = row._meta.db_table
    identify = IDENTIFY.get(table)
    if identify is not None:
        return identify(row)
    parts = []
    for field in row._meta.local_fields:
        if field.name in LABEL_ONLY:
            value = getattr(row, field.name if field.is_relation else field.attname)
            if value in (None, ""):
                continue
            parts.append(str(value))
    label = TABLE_LABELS.get(table, table.replace("_", " "))
    return f"{label} — {', '.join(parts)}" if parts else label


TABLE_LABELS = {
    "minimum_wage_rate": "Minimum wage",
    "paye_tax_bracket": "PAYE tax bracket",
    "paye_rebate": "PAYE rebate",
    "medical_tax_credit_rate": "Medical scheme tax credit",
    "statutory_parameter": "Statutory parameter",
    "parental_leave_quantum": "Parental leave quantum",
    "adoption_age_limit": "Adoption age limit",
    "leave_rule_set": "Leave rules",
    "working_time_rule_set": "Working time rules",
    "termination_rule_set": "Termination rules",
    "termination_notice_band": "Notice band",
    "public_holiday": "Public holiday",
    "sars_source_code": "SARS source code",
}

#: A field's own words, where the column name is not prose a person can read.
FIELD_LABELS = {
    "value_numeric": "value",
    "value_text": "value (text)",
    "marginal_rate_pct": "marginal rate",
    "base_tax": "cumulative tax at the bottom of the bracket",
    "income_from": "income from",
    "income_to": "income to",
    "weekly_rate_45h": "weekly rate (45h)",
    "monthly_rate_45h": "monthly rate (45h)",
    "daily_rate_9h": "daily rate (9h)",
    "annual_amount": "annual amount",
    "tax_threshold_annual": "annual tax threshold",
    "min_age": "minimum age",
    "code_group": "code group",
    "description": "description",
}


def field_label(field) -> str:
    return FIELD_LABELS.get(field.name, field.name.replace("_", " "))


@dataclasses.dataclass(frozen=True)
class Line:
    """One figure a person has to check, and where to check it."""

    document: str
    clause: str
    source_url: str
    version: str
    table: str
    description: str
    value: str
    effective_from: str
    effective_to: str
    key: str

    @property
    def sort_key(self):
        return (self.document, self.clause, self.effective_from, self.table, self.key)


def row_versions() -> dict[tuple[str, int], str]:
    """Which reference version loaded each row, recovered from the fixtures.

    No cited table carries a version foreign key (D-252), so this replays every
    shipped fixture through the loader's own ``locate_row()``. A row present in
    the database but in no fixture — there should be none — is reported as
    unknown rather than guessed at.
    """
    directory = Path(settings.BASE_DIR) / FIXTURE_DIRECTORY
    found: dict[tuple[str, int], str] = {}
    for name in FIXTURE_ORDER:
        path = directory / name
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        label = document["version_label"]
        for table_name, rows in document.get("tables", {}).items():
            spec = TABLES.get(table_name)
            if spec is None:
                continue
            for index, raw in enumerate(rows, start=1):
                try:
                    instance = locate_row(spec, dict(raw), where=f"{name}:{table_name}[{index}]")
                except ReferenceDataLoadError:
                    # The fixture names a sector, grade or tax year that is not in
                    # this database, so its rows are not in this database either.
                    # Normal on a test database holding one fixture, and on a
                    # partially loaded one; either way there is nothing to map.
                    continue
                if instance is not None:
                    found[(table_name, instance.pk)] = label
    return found


def lines() -> list[Line]:
    """Every loaded figure, as a line the workbook can print."""
    versions = row_versions()
    out: list[Line] = []
    for table_name, spec in TABLES.items():
        if not spec.requires_citation:
            continue
        fields = [
            f
            for f in spec.model._meta.local_fields
            if f.concrete and f.name not in SCAFFOLDING and f.name not in LABEL_ONLY
        ]
        dated = spec.is_effective_dated
        for row in spec.model.objects.all():
            document, clause = split_citation(row.source_reference)
            version = versions.get((table_name, row.pk), "(not in any fixture)")
            description = describe(row)
            for field in fields:
                if field.name in {"effective_from", "effective_to"}:
                    continue
                value = format_value(row, field)
                if value == "":
                    continue
                out.append(
                    Line(
                        document=document,
                        clause=clause,
                        source_url=row.source_url or "",
                        version=version,
                        table=table_name,
                        description=f"{description} — {field_label(field)}",
                        value=value,
                        effective_from=(
                            f"{row.effective_from:%Y-%m-%d}" if dated and row.effective_from else ""
                        ),
                        effective_to=(
                            f"{row.effective_to:%Y-%m-%d}" if dated and row.effective_to else ""
                        ),
                        key=f"{table_name}:{row.pk}:{field.name}",
                    )
                )
    out.sort(key=lambda line: line.sort_key)
    return out


def parse_key(key: str) -> tuple[str, int, str]:
    """Split a row key back into the table, primary key and field it names."""
    match = re.fullmatch(r"([a-z_]+):(\d+):([a-z0-9_]+)", key.strip())
    if match is None:
        raise ValueError(f"'{key}' is not a row key of the form table:pk:field.")
    return match.group(1), int(match.group(2)), match.group(3)


def current_value(key: str) -> str:
    """The value the database holds for a row key, formatted identically.

    The comparison ``importverification`` makes runs through this, so a figure
    edited in the spreadsheet differs from the loaded one and the version is
    refused (D-251).
    """
    table_name, pk, field_name = parse_key(key)
    spec = TABLES.get(table_name)
    if spec is None:
        raise ValueError(f"'{table_name}' is not a reference table.")
    row = spec.model.objects.filter(pk=pk).first()
    if row is None:
        raise ValueError(f"{table_name} row {pk} no longer exists.")
    return format_value(row, spec.model._meta.get_field(field_name))


def latest_checks() -> dict[str, ReferenceFigureCheck]:
    """The current state of every figure, keyed by row key.

    ``reference_figure_check`` is append-only, so a figure that was queried,
    corrected and then checked has three rows and the last one is what it is now
    (D-256). Ordered by ``recorded_at`` and then by id, because two rows written
    by the same import share a timestamp to the microsecond often enough to
    matter, and the later id is the later row.
    """
    found: dict[str, ReferenceFigureCheck] = {}
    for check in ReferenceFigureCheck.objects.select_related("checked_by").order_by(
        "recorded_at", "id"
    ):
        found[check.row_key] = check
    return found


@dataclasses.dataclass(frozen=True)
class Progress:
    """How far one source document or one reference version has got."""

    name: str
    figures: int
    checked: int
    queried: int

    @property
    def outstanding(self) -> int:
        return self.figures - self.checked - self.queried


def _progress(grouped: dict[str, list[Line]], checks: dict[str, ReferenceFigureCheck]):
    out = []
    for name in sorted(grouped):
        lines_for = grouped[name]
        checked = sum(
            1
            for line in lines_for
            if (check := checks.get(line.key)) is not None
            and check.outcome == ReferenceFigureCheck.Outcome.CHECKED
        )
        queried = sum(
            1
            for line in lines_for
            if (check := checks.get(line.key)) is not None
            and check.outcome == ReferenceFigureCheck.Outcome.QUERIED
        )
        out.append(Progress(name, len(lines_for), checked, queried))
    return out


def progress_by_document(lines_: list[Line], checks: dict[str, ReferenceFigureCheck]):
    """Per source document, counted from the DATABASE and not from a spreadsheet.

    The summary sheet has to be right on a freshly written file that nobody has
    opened. A COUNTIFS over the Figures sheet is not: openpyxl writes the
    formula and only Excel evaluates it, so the file reads as zero everywhere
    until somebody opens it — which is exactly the moment the answer stops being
    useful for deciding whether to bother.
    """
    grouped: dict[str, list[Line]] = {}
    for line in lines_:
        grouped.setdefault(line.document, []).append(line)
    return _progress(grouped, checks)


def progress_by_version(lines_: list[Line], checks: dict[str, ReferenceFigureCheck]):
    grouped: dict[str, list[Line]] = {}
    for line in lines_:
        grouped.setdefault(line.version, []).append(line)
    return {item.name: item for item in _progress(grouped, checks)}


def fully_checked_versions(lines_: list[Line] | None = None) -> dict[str, list[str]]:
    """Versions whose every figure has a CHECKED record, and who checked them.

    Read from the database rather than from whichever workbook is in front of
    us, so two people working two halves on two evenings complete a version
    between them without either file ever holding the whole of it.
    """
    lines_ = lines() if lines_ is None else lines_
    checks = latest_checks()
    grouped: dict[str, list[Line]] = {}
    for line in lines_:
        grouped.setdefault(line.version, []).append(line)

    complete = {}
    for label, group in grouped.items():
        states = [checks.get(line.key) for line in group]
        if any(
            state is None or state.outcome != ReferenceFigureCheck.Outcome.CHECKED
            for state in states
        ):
            continue
        complete[label] = sorted({state.checked_by.email for state in states})
    return complete


def version_state() -> list[dict]:
    """Every reference version with what the workbook needs to say about it."""
    counts: dict[str, int] = {}
    for line in lines():
        counts[line.version] = counts.get(line.version, 0) + 1

    out = []
    for version in ReferenceDataVersion.objects.order_by("applies_from", "version_label"):
        verifier = version.verified_by_user.email if version.verified_by_user else ""
        loader = version.loaded_by_user.email if version.loaded_by_user else ""
        out.append(
            {
                "label": version.version_label,
                "loaded_by": loader,
                "applies_from": version.applies_from,
                "figures": counts.get(version.version_label, 0),
                "verified": bool(version.verified_at),
                "verified_by": verifier,
                "machine_verified": verifier.endswith("@labourmax.invalid"),
                "current_through": version.data_current_through,
                "superseded": version.superseded_by_set.exists()
                if hasattr(version, "superseded_by_set")
                else False,
            }
        )
    return out
