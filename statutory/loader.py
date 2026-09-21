"""The reference data loader — how statutory figures get into the database.

Decision D-51: statutory updates are **not automatic**. A robot that scrapes a
gazette and writes rates into a payroll system is a robot that will one day write a
wrong rate into every tenant at once, on a Sunday, unattended. The load is a human
act with a file, a second person's verification, and a date beyond which the system
admits it does not know — which is the staleness guard in ``staleness.py``.

What this module is, then, is the narrow gate that act passes through. It reads one
JSON document describing one load, and it refuses the entire file rather than
accepting part of it. Seven rules, each of which exists because the alternative is a
plausible-looking wrong number:

1. **All or nothing.** One transaction. A half-loaded rate table is worse than an
   empty one: an empty one fails loudly on the first lookup, a half-loaded one
   answers three questions correctly and the fourth one wrong.

2. **No citation, no load.** Checked in Python before the database is touched, so
   the error names the table and the row rather than a constraint. The CHECK
   constraints are still there and still enforce it — this is the layer that
   produces a message a person can act on.

3. **Nothing is ever updated.** A statutory value that changes is a new row with a
   new effective date; the old row stays exactly as it was, because a payroll run
   from 2026 must still resolve to the 2026 figure in 2031. A fixture row that
   matches an existing natural key but carries different values is a *refusal*, not
   an update. That refusal is the single most valuable thing this loader does.

4. **Foreign keys by natural key.** Fixtures reference a sector as
   ``"DOMESTIC"``, never as ``7``. Integer ids differ between the development
   database, CI and production, and a fixture carrying them silently loads the wrong
   rows in the one environment that matters.

5. **Superseding is declared, not inferred.** Loading a new rate while the previous
   one is still open-ended violates the overlap exclusion constraint, and the fix —
   closing the old period — is a modification of already-verified data. So it
   happens only when the fixture says ``closes_open_periods_from``, and every row
   closed is listed in the report for the verifier to check.

6. **The loader never verifies its own load.** ``verified_at``,
   ``verified_by_user`` and ``data_current_through`` are untouched here. They are
   set by a second person through ``manage.py verifystatutory``, against the source
   document. The database enforces the ordering as well, but the loader does not go
   near it.

7. **The fixture is fingerprinted.** A SHA-256 over the canonical form of the data
   is stored on the version. Re-running an identical file is a no-op; re-running a
   *changed* file under a label that has already been loaded is refused. A fixture
   quietly edited after loading is then detectable rather than theoretical.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.db import models, transaction

from statutory.models import (
    AdoptionAgeLimit,
    Bank,
    BankBranch,
    JobGrade,
    LeaveRuleSet,
    MedicalTaxCreditRate,
    MinimumWageRate,
    MunicipalityAreaMap,
    ParentalLeaveQuantum,
    PayeRebate,
    PayeTaxBracket,
    PublicHoliday,
    ReferenceDataVersion,
    SarsSourceCode,
    Sector,
    SectorArea,
    StatutoryParameter,
    StatutoryWatchItem,
    TaxYear,
    TerminationNoticeBand,
    TerminationRuleSet,
    WorkingTimeRuleSet,
)


class ReferenceDataLoadError(Exception):
    """The load was refused. Nothing was written.

    Every message names the table and the row, because the person reading it is
    holding a gazette and a spreadsheet, not a stack trace.
    """


@dataclass(frozen=True)
class TableSpec:
    """How one table is loaded from a fixture.

    ``natural_key`` is what identifies a row *within its own table* — the fields a
    second load would have to repeat to mean "this same row". It is what makes rule
    3 possible: same natural key, different values, refuse.

    ``references`` maps a field to the model and lookup field its natural key is
    resolved against, which is rule 4.

    ``composite_references`` is the same idea for a parent whose OWN natural key is
    more than one field — ``termination_notice_band`` -> ``termination_rule_set`` is
    the first of these, since a rule set's identity is ``(sector, effective_from)``,
    not one column the way ``tax_year.label`` is. The tuple names which
    ALREADY-RESOLVED fields on this row (in the order they were resolved — plain
    references first, so ``sector`` is already a ``Sector`` instance by the time a
    composite lookup reads it) form the parent's lookup, and they are consumed:
    popped from the row once used, because they are not columns on the child.

    ``scope`` is the set of fields that define "the same thing over time" for an
    effective-dated table — the columns that must match for one period to supersede
    another. It is deliberately the same tuple the exclusion constraint uses.
    """

    model: type[models.Model]
    natural_key: tuple[str, ...]
    references: dict[str, tuple[type[models.Model], str]] = field(default_factory=dict)
    composite_references: dict[str, tuple[type[models.Model], tuple[str, ...]]] = field(
        default_factory=dict
    )
    #: Composite-lookup fields a fixture may omit, where omitting one MEANS NULL.
    #: Named one by one, never inferred from the parent field being nullable:
    #: ``sector`` is nullable too — that is the BCEA default row — so inferring it
    #: would silently attach a band whose sector was left out by mistake to the
    #: BCEA default instead of refusing. ``sector_area`` arrived with the BCCCI
    #: Main Agreement (D-240) and every fixture written before it names no area,
    #: which is exactly what a sector-wide row is.
    optional_lookup_fields: frozenset[str] = frozenset()
    scope: tuple[str, ...] = ()

    @property
    def is_effective_dated(self) -> bool:
        return "effective_from" in {f.name for f in self.model._meta.get_fields()}

    @property
    def requires_citation(self) -> bool:
        return "source_reference" in {f.name for f in self.model._meta.get_fields()}


# Order is load order, and it is dependency order: nothing may reference a table
# that appears below it.
TABLES: dict[str, TableSpec] = {
    "sector": TableSpec(Sector, natural_key=("code",)),
    "sector_area": TableSpec(
        SectorArea,
        natural_key=("sector", "code"),
        references={"sector": (Sector, "code")},
    ),
    "job_grade": TableSpec(
        JobGrade,
        natural_key=("sector", "code"),
        references={"sector": (Sector, "code")},
    ),
    "municipality_area_map": TableSpec(
        MunicipalityAreaMap,
        natural_key=("municipality_name", "effective_from"),
        references={"sector_area": (SectorArea, "code")},
        scope=("municipality_name",),
    ),
    "minimum_wage_rate": TableSpec(
        MinimumWageRate,
        natural_key=("sector", "sector_area", "job_grade", "hours_band", "effective_from"),
        references={
            "sector": (Sector, "code"),
            "sector_area": (SectorArea, "code"),
            "job_grade": (JobGrade, "code"),
        },
        scope=("sector", "sector_area", "job_grade", "hours_band"),
    ),
    "tax_year": TableSpec(TaxYear, natural_key=("label",)),
    "paye_tax_bracket": TableSpec(
        PayeTaxBracket,
        natural_key=("tax_year", "bracket_order"),
        references={"tax_year": (TaxYear, "label")},
    ),
    "paye_rebate": TableSpec(
        PayeRebate,
        natural_key=("tax_year", "rebate_type"),
        references={"tax_year": (TaxYear, "label")},
    ),
    "medical_tax_credit_rate": TableSpec(
        MedicalTaxCreditRate,
        natural_key=("tax_year",),
        references={"tax_year": (TaxYear, "label")},
    ),
    # Scoped since the BCCCI Main Agreement (D-236): one parameter code can carry
    # a different binding figure per sector and area, so the natural key and the
    # scope both widen to match the table's own unique constraint. Every row
    # loaded before this leaves both references absent, which resolves to NULL.
    "statutory_parameter": TableSpec(
        StatutoryParameter,
        natural_key=("parameter_code", "sector", "sector_area", "effective_from"),
        references={
            "sector": (Sector, "code"),
            "sector_area": (SectorArea, "code"),
        },
        scope=("parameter_code", "sector", "sector_area"),
    ),
    # Van Wyk's interim quantum, and the adoption age limit it retains. Two
    # tables because they LAPSE IN OPPOSITE DIRECTIONS (D-203): the quantum
    # vanishes and the resolver refuses; the age limit is replaced by a row
    # saying there is none.
    "parental_leave_quantum": TableSpec(
        ParentalLeaveQuantum,
        natural_key=("effective_from",),
        scope=(),
    ),
    "adoption_age_limit": TableSpec(
        AdoptionAgeLimit,
        natural_key=("effective_from",),
        scope=(),
    ),
    "leave_rule_set": TableSpec(
        LeaveRuleSet,
        natural_key=("sector", "sector_area", "effective_from"),
        references={"sector": (Sector, "code"), "sector_area": (SectorArea, "code")},
        scope=("sector", "sector_area"),
    ),
    "working_time_rule_set": TableSpec(
        WorkingTimeRuleSet,
        natural_key=("sector", "sector_area", "effective_from"),
        references={"sector": (Sector, "code"), "sector_area": (SectorArea, "code")},
        scope=("sector", "sector_area"),
    ),
    "termination_rule_set": TableSpec(
        TerminationRuleSet,
        natural_key=("sector", "sector_area", "effective_from"),
        references={"sector": (Sector, "code"), "sector_area": (SectorArea, "code")},
        scope=("sector", "sector_area"),
    ),
    # The rule set a band hangs off is now identified by sector AND area
    # (D-240): Area B has its own notice regime and Areas A and C keep SD1's.
    "termination_notice_band": TableSpec(
        TerminationNoticeBand,
        natural_key=("termination_rule_set", "sequence"),
        references={"sector": (Sector, "code"), "sector_area": (SectorArea, "code")},
        composite_references={
            "termination_rule_set": (
                TerminationRuleSet,
                ("sector", "sector_area", "effective_from"),
            )
        },
        optional_lookup_fields=frozenset({"sector_area"}),
    ),
    "public_holiday": TableSpec(PublicHoliday, natural_key=("country_code", "holiday_date")),
    "sars_source_code": TableSpec(
        SarsSourceCode,
        natural_key=("code",),
        references={
            "valid_from_tax_year": (TaxYear, "label"),
            "valid_to_tax_year": (TaxYear, "label"),
        },
    ),
    "bank": TableSpec(Bank, natural_key=("name",)),
    "bank_branch": TableSpec(
        BankBranch,
        natural_key=("bank", "branch_code"),
        references={"bank": (Bank, "name")},
    ),
    "statutory_watch_item": TableSpec(StatutoryWatchItem, natural_key=("watch_code",)),
}


# The fixtures in reference/, in dependency order.
#
# This is a property of the data, not of the filenames: the rule set and contract
# cleaning fixtures reference sectors by code, and those sectors are created by the
# first file. Alphabetical order puts them the wrong way round, so a shell glob loads
# them in an order the loader correctly refuses - which is how this list came to
# exist. `loadstatutory --all` reads it; `test_every_fixture_is_in_the_load_order`
# fails if a new fixture is added to the directory and not to this list.
FIXTURE_ORDER = [
    "ref-2026.03.01.json",
    "ref-2026.03.01-rules.json",
    "ref-2026.03.01-sd1.json",
    "ref-2026.03.01-notice-bands.json",
    "ref-2026.03.01-codes.json",
    "ref-2026.03.01-banks.json",
    "ref-2026.03.01-employment.json",
    "ref-2026.03.01-remuneration.json",
    "ref-2026.03.01-lumpsum.json",
    "ref-2026.03.01-leave.json",
    "ref-2026.03.01-sick-accrual.json",
    "ref-2026.03.01-parental.json",
    "ref-2026.03.01-leave-pay.json",
    "ref-2026.03.01-termination.json",
    # The predecessor first: it closes on the 2026 agreement's own start date, so
    # loading it after would be loading a row that ends where a loaded row begins.
    # Order here is dependency order, and both need the sectors from the first file.
    "ref-2023.04.01-bccci.json",
    "ref-2023.04.01-bccci-rules.json",
    "ref-2023.04.01-bccci-termination.json",
    "ref-2023.04.01-bccci-leave-types.json",
    "ref-2023.04.01-bccci-probation.json",
    "ref-2026.04.01-bccci.json",
    "ref-2026.04.01-bccci-rules.json",
    "ref-2026.04.01-bccci-notice.json",
    "ref-2026.04.01-bccci-maternity.json",
    "ref-2026.04.01-bccci-leave-types.json",
]

FIXTURE_DIRECTORY = "reference"


@dataclass
class LoadReport:
    """What the load did, in the terms the verifier will check it in."""

    version_label: str
    created: dict[str, int] = field(default_factory=dict)
    unchanged: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    closed_periods: list[str] = field(default_factory=list)
    already_loaded: bool = False
    superseded: str = ""

    @property
    def total_created(self) -> int:
        return sum(self.created.values())

    def lines(self) -> list[str]:
        out = [f"Reference data version {self.version_label}"]
        if self.already_loaded:
            out.append("  Already loaded with an identical fixture. Nothing written.")
            return out
        for table in TABLES:
            created = self.created.get(table, 0)
            unchanged = self.unchanged.get(table, 0)
            if created or unchanged:
                out.append(f"  {table:<26} {created:>4} loaded, {unchanged:>4} already present")
        for line in self.closed_periods:
            out.append(f"  closed  {line}")
        if self.superseded:
            for table, count in self.updated.items():
                out.append(f"  {table:<26} {count:>4} citation(s) re-encoded")
            out.append(f"  Supersedes {self.superseded}, which is kept and stays readable.")
            out.append("  NOT verified. A second person must run: manage.py verifystatutory")
            return out
        out.append(f"  {self.total_created} rows loaded in total.")
        out.append("  NOT verified. A second person must run: manage.py verifystatutory")
        return out


def fingerprint(payload: Any) -> str:
    """SHA-256 over the canonical JSON form, so key order cannot change the result."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_date(value: Any, *, where: str) -> datetime.date:
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ReferenceDataLoadError(
            f"{where}: '{value}' is not a date (expected YYYY-MM-DD)."
        ) from exc


def _resolve_references(spec: TableSpec, row: dict[str, Any], *, where: str) -> dict[str, Any]:
    """Turn natural keys into instances, refusing anything that does not exist.

    A fixture naming a sector that has not been loaded is a typo or a missing
    section, and both are worth stopping the whole file for.
    """
    resolved = dict(row)
    for field_name, (model, lookup) in spec.references.items():
        if field_name not in resolved:
            continue
        value = resolved[field_name]
        if value is None:
            continue
        try:
            resolved[field_name] = model.objects.get(**{lookup: value})
        except model.DoesNotExist as exc:
            raise ReferenceDataLoadError(
                f"{where}: no {model._meta.db_table} with {lookup}='{value}'. "
                f"Load it earlier in the same file, or correct the reference."
            ) from exc
        except model.MultipleObjectsReturned as exc:
            raise ReferenceDataLoadError(
                f"{where}: '{value}' matches more than one {model._meta.db_table}. "
                f"A fixture refers to rows by natural key, so these codes have to be "
                f"unique across sectors - prefix them if two sectors reuse one."
            ) from exc

    for field_name, (model, lookup_fields) in spec.composite_references.items():
        kwargs = {}
        for key in lookup_fields:
            if key not in resolved:
                if key in spec.optional_lookup_fields:
                    kwargs[f"{key}__isnull"] = True
                    continue
                raise ReferenceDataLoadError(
                    f"{where}: '{key}' is required to resolve '{field_name}' and is missing."
                )
            if resolved[key] is None:
                kwargs[f"{key}__isnull"] = True
            else:
                kwargs[key] = resolved[key]
        try:
            resolved[field_name] = model.objects.get(**kwargs)
        except model.DoesNotExist as exc:
            described = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            raise ReferenceDataLoadError(
                f"{where}: no {model._meta.db_table} with {described}. "
                f"Load it earlier in the same file, or correct the reference."
            ) from exc
        except model.MultipleObjectsReturned as exc:
            raise ReferenceDataLoadError(
                f"{where}: more than one {model._meta.db_table} matches {kwargs}, which "
                f"should not be possible - its own natural key is meant to be unique."
            ) from exc
        # The lookup fields were needed only to find the parent; they are not
        # columns on this table, so they do not survive into the final values.
        for key in lookup_fields:
            resolved.pop(key, None)

    return resolved


def _differences(instance: models.Model, values: dict[str, Any]) -> list[str]:
    """Fields where an existing row disagrees with the fixture."""
    out = []
    for name, new in values.items():
        field_object = instance._meta.get_field(name)
        current = getattr(instance, field_object.attname if field_object.is_relation else name)
        comparable = new.pk if isinstance(new, models.Model) else new
        if isinstance(field_object, models.DecimalField) and comparable is not None:
            from decimal import Decimal

            comparable = Decimal(str(comparable))
            # Numerically, not as text: 0 and 0.00 are the same FIGURE, and a
            # fixture writing one where the column holds the other is not an
            # edit. Comparing str() reported a figure change that was only a
            # difference in trailing zeros - found by --supersede refusing a
            # citation-only re-encoding of the notice bands (O-21).
            if current is not None:
                if Decimal(str(current)) != comparable:
                    out.append(f"{name}: database has {current!r}, fixture has {comparable!r}")
                continue
        if isinstance(field_object, (models.DateField, models.DateTimeField)) and isinstance(
            comparable, str
        ):
            comparable = _as_date(comparable, where=name)
        if str(current) != str(comparable):
            out.append(f"{name}: database has {current!r}, fixture has {comparable!r}")
    return out


#: Fields that carry the READING rather than the figure. A re-encoding may change
#: these and nothing else — see ``supersede`` in ``load_reference_data``.
PROSE_FIELDS = frozenset({"source_reference", "source_url", "notes"})

#: What may change on a band that the DATABASE currently records as CONTESTED,
#: and on no other row (D-277).
#:
#: A contested band is the one row in this schema that explicitly holds NO
#: figure: ``notice_value`` is NULL and ``is_contested`` says why (D-241). So
#: filling it in moves nothing — there was never a figure there to edit, which
#: is the whole of what "a statutory value is never edited" protects. Reading a
#: clause again and finding it resolvable is exactly the event this codebase
#: keeps having (D-224, D-248, D-275), and before this it had nowhere to land:
#: ``--supersede`` refused it as a figure change and an ordinary load refused it
#: as an edit, so a corrected reading could only be got in by deleting rows.
#:
#: The guard that matters is not this list but the one already above it: a
#: VERIFIED version refuses to be superseded at all without ``--supersede-
#: verified``. Somebody who ticked a contested row ticked "the instrument really
#: is irreconcilable", and that tick may not be quietly falsified.
CONTESTED_RESOLUTION_FIELDS = frozenset(
    {"is_contested", "contested_reason", "notice_value", "notice_unit", "probation_condition"}
)


class SupersedeRefusedError(ReferenceDataLoadError):
    """The new file is not a re-encoding of the old version."""


def _supersede_rows(document, *, old_label: str, report: LoadReport) -> list[tuple]:
    """Check the new file against what is already loaded, and return the prose
    updates to apply. Refuses on anything that is not prose.

    A re-encoding says the same statutory facts differently. So every row in the
    file must already exist (adding one is new data), every figure must match
    (a changed figure is a new gazette and gets an ordinary load with its own
    effective date), and only the citation, source URL and notes may differ.
    """
    figure_changes: list[str] = []
    missing: list[str] = []
    updates: list[tuple] = []
    new_rows: list[tuple] = []
    resolved_a_contested_band = False

    for table_name, spec in TABLES.items():
        rows = document["tables"].get(table_name)
        if not rows:
            continue
        for index, raw in enumerate(rows, start=1):
            where = f"{table_name}[{index}]"
            values = _resolve_references(spec, raw, where=where)
            lookup = {}
            for key in spec.natural_key:
                if key in values:
                    lookup[key] = values[key]
                    continue
                field_object = spec.model._meta.get_field(key)
                if field_object.has_default():
                    lookup[key] = field_object.get_default()
                elif field_object.null:
                    lookup[key] = None
            existing = spec.model.objects.filter(**lookup).first()
            if existing is None:
                if _is_a_conditional_twin(values):
                    # The other lane of a band being resolved in this same load
                    # (D-277). It adds a row, which a re-encoding may not do —
                    # but a conditional band is meaningless alone: stating "one
                    # week while on probation" without its off-probation twin
                    # leaves every other employee with no band at all, and
                    # check_notice_bands() reads that as the gap it is. So the
                    # pair arrives together or the load is refused below.
                    new_rows.append((spec, raw, where))
                    continue
                identity = ", ".join(f"{k}={v}" for k, v in lookup.items() if v is not None)
                missing.append(f"  - {where}: {identity}")
                continue

            was_contested = bool(getattr(existing, "is_contested", False))
            prose = {}
            for difference in _differences(existing, values):
                field_name = difference.split(":", 1)[0].strip()
                if field_name in PROSE_FIELDS:
                    prose[field_name] = values[field_name]
                elif was_contested and field_name in CONTESTED_RESOLUTION_FIELDS:
                    prose[field_name] = values[field_name]
                    resolved_a_contested_band = True
                else:
                    figure_changes.append(f"  - {where}.{difference}")
            if prose:
                updates.append((existing, prose))

    if figure_changes:
        raise SupersedeRefusedError(
            f"--supersede {old_label} refused: a FIGURE changed, so this is not a "
            f"re-encoding. A changed figure is a new gazette — load it as its own "
            f"version with its own effective date:\n" + "\n".join(figure_changes)
        )
    if missing:
        raise SupersedeRefusedError(
            f"--supersede {old_label} refused: the file carries row(s) the database does "
            f"not have, which is new data rather than a re-encoding:\n" + "\n".join(missing)
        )
    if new_rows and not resolved_a_contested_band:
        added = "\n".join(f"  - {where}" for _, _, where in new_rows)
        raise SupersedeRefusedError(
            f"--supersede {old_label} refused: the file adds a probation-conditional "
            f"band, but resolves no contested band. A conditional band is only ever the "
            f"other lane of a band whose contradiction is being settled; on its own it "
            f"is new data and belongs in its own version:\n" + added
        )
    return updates, new_rows


def _is_a_conditional_twin(values: dict) -> bool:
    """Is this fixture row the other lane of a band being resolved (D-277)?

    Narrow on purpose. An unconditional row the database does not have is new
    data however it arrives, and stays refused.
    """
    condition = values.get("probation_condition")
    return condition in {"on_probation", "off_probation"}


@transaction.atomic
def load_reference_data(
    document: dict[str, Any],
    *,
    loaded_by=None,
    supersede: str = "",
    reason: str = "",
    supersede_verified: bool = False,
) -> LoadReport:
    """Load one reference data document. Refuses the whole file on any problem.

    Returns a report rather than printing one, so the management command, a test and
    a future admin screen all see the same result.
    """
    for required in ("version_label", "applies_from", "tables"):
        if required not in document:
            raise ReferenceDataLoadError(f"The fixture has no '{required}'.")

    label = str(document["version_label"])
    applies_from = _as_date(document["applies_from"], where="applies_from")
    checksum = fingerprint(document["tables"])

    unknown = set(document["tables"]) - set(TABLES)
    if unknown:
        raise ReferenceDataLoadError(
            f"Unknown table(s) in the fixture: {', '.join(sorted(unknown))}. "
            f"Loadable tables are: {', '.join(TABLES)}."
        )

    report = LoadReport(version_label=label)

    superseded = None
    if supersede:
        if not reason.strip():
            raise ReferenceDataLoadError(
                "--supersede requires --reason: a re-encoding with no stated reason is "
                "indistinguishable from an edit somebody made quietly."
            )
        superseded = ReferenceDataVersion.objects.filter(version_label=supersede).first()
        if superseded is None:
            raise ReferenceDataLoadError(f"No loaded version '{supersede}' to supersede.")
        if superseded.verified_at is not None and not supersede_verified:
            raise ReferenceDataLoadError(
                f"Version '{supersede}' is VERIFIED (by "
                f"{superseded.verified_by_user or 'somebody'} on "
                f"{superseded.verified_at:%d %B %Y}). Superseding it silently un-verifies "
                f"the data a payroll run was computed against. Pass --supersede-verified "
                f"to do it deliberately; the version then needs verifying again."
            )
        if hasattr(superseded, "superseded_by"):
            raise ReferenceDataLoadError(
                f"Version '{supersede}' was already superseded by "
                f"'{superseded.superseded_by.version_label}'."
            )

    existing = ReferenceDataVersion.objects.filter(version_label=label).first()
    if existing is not None:
        if existing.checksum and existing.checksum != checksum:
            raise ReferenceDataLoadError(
                f"Version '{label}' has already been loaded from a different fixture. "
                f"A statutory value is never edited in place - issue a new version with "
                f"a new effective date instead."
            )
        report.already_loaded = True
        return report

    if superseded is not None:
        # Checked BEFORE anything is written: a refusal writes nothing at all.
        updates, new_rows = _supersede_rows(document, old_label=supersede, report=report)
        version = ReferenceDataVersion.objects.create(
            version_label=label,
            applies_from=applies_from,
            description=document.get("description", ""),
            checksum=checksum,
            loaded_by_user=loaded_by,
            supersedes=superseded,
            supersede_reason=reason.strip(),
        )
        for row, prose in updates:
            for field_name, value in prose.items():
                setattr(row, field_name, value)
            row.save(update_fields=[*prose, "updated_at"])
            report.updated[row._meta.db_table] = report.updated.get(row._meta.db_table, 0) + 1
        for spec, raw, where in new_rows:
            _load_row(spec, raw, where=where, closes_from=None, report=report)
        report.superseded = supersede
        return report

    version = ReferenceDataVersion.objects.create(
        version_label=label,
        applies_from=applies_from,
        description=document.get("description", ""),
        checksum=checksum,
        loaded_by_user=loaded_by,
    )

    closes_from = document.get("closes_open_periods_from")
    closes_from = _as_date(closes_from, where="closes_open_periods_from") if closes_from else None

    for table_name, spec in TABLES.items():
        rows = document["tables"].get(table_name)
        if not rows:
            continue
        for index, raw in enumerate(rows, start=1):
            where = f"{table_name}[{index}]"
            _load_row(spec, raw, where=where, closes_from=closes_from, report=report)

    version.refresh_from_db()
    return report


def natural_key_lookup(spec: TableSpec, values: dict, *, where: str) -> dict:
    """The filter that identifies ``values``' row within its own table.

    Evaluated exactly as the row would be CREATED, or an omitted nullable scope
    column turns a duplicate into a second row: a minimum wage fixture naming
    only a sector means "no area, no grade, all hours", and the lookup has to say
    so explicitly rather than leave those columns out.

    Shared with ``statutory/verification.py`` so the verification workbook matches
    a fixture row to its database row the same way the loader does. Two matchers
    would be two answers to "is this the same row", and they drift.
    """
    lookup = {}
    for key in spec.natural_key:
        if key in values:
            lookup[key] = values[key]
            continue
        field_object = spec.model._meta.get_field(key)
        if field_object.has_default():
            lookup[key] = field_object.get_default()
        elif field_object.null:
            lookup[key] = None
        else:
            raise ReferenceDataLoadError(
                f"{where}: '{key}' is part of this table's identity and is missing."
            )
    return lookup


def locate_row(spec: TableSpec, raw: dict, *, where: str):
    """The database row a fixture row refers to, or None if it was never loaded.

    Read-only: resolves the fixture's natural keys to instances and looks the row
    up. Writes nothing, so it is safe to call over every shipped fixture.
    """
    values = _resolve_references(spec, raw, where=where)
    for name in list(values):
        field_object = spec.model._meta.get_field(name)
        is_plain_date = isinstance(field_object, models.DateField) and not isinstance(
            field_object, models.DateTimeField
        )
        if is_plain_date and values[name] is not None:
            values[name] = _as_date(values[name], where=f"{where}.{name}")
    lookup = natural_key_lookup(spec, values, where=where)
    return spec.model.objects.filter(**lookup).first()


def _load_row(spec: TableSpec, raw: dict, *, where: str, closes_from, report: LoadReport) -> None:
    if not isinstance(raw, dict):
        raise ReferenceDataLoadError(f"{where}: expected an object, found {type(raw).__name__}.")

    if spec.requires_citation and not str(raw.get("source_reference", "")).strip():
        raise ReferenceDataLoadError(
            f"{where}: no source_reference. Every statutory figure carries a citation "
            f"precise enough to find it again - a gazette notice, an Act and section, a "
            f"SARS table, or a collective agreement clause."
        )

    values = _resolve_references(spec, raw, where=where)

    for name in list(values):
        try:
            field_object = spec.model._meta.get_field(name)
        except Exception as exc:
            raise ReferenceDataLoadError(
                f"{where}: '{name}' is not a column of {spec.model._meta.db_table}."
            ) from exc
        is_plain_date = isinstance(field_object, models.DateField) and not isinstance(
            field_object, models.DateTimeField
        )
        if is_plain_date and values[name] is not None:
            values[name] = _as_date(values[name], where=f"{where}.{name}")

    lookup = natural_key_lookup(spec, values, where=where)

    table = spec.model._meta.db_table
    existing = spec.model.objects.filter(**lookup).first()
    if existing is not None:
        differences = _differences(existing, values)
        if differences:
            raise ReferenceDataLoadError(
                f"{where}: a row already exists with this identity but different values:\n    "
                + "\n    ".join(differences)
                + "\n  A statutory value is never edited. Supersede it with a new "
                "effective-dated row instead."
            )
        report.unchanged[table] = report.unchanged.get(table, 0) + 1
        return

    if spec.is_effective_dated and closes_from is not None:
        _close_open_period(spec, lookup, closes_from=closes_from, report=report)

    spec.model.objects.create(**values)
    report.created[table] = report.created.get(table, 0) + 1


def _close_open_period(spec: TableSpec, identity: dict, *, closes_from, report) -> None:
    """End the open-ended row this one supersedes, and say so in the report.

    Only reached when the fixture declared ``closes_open_periods_from``. The scope is
    the same tuple the table's exclusion constraint uses, and it is read from the
    resolved identity rather than from the raw fixture row — so a wage fixture naming
    only a sector closes the sector's "no area, no grade, all hours" row, which is
    the row it would otherwise have collided with.
    """
    scope = {name: identity[name] for name in spec.scope}
    open_rows = spec.model.objects.filter(effective_to__isnull=True, **scope).exclude(
        effective_from__gte=closes_from
    )
    for row in open_rows:
        row.effective_to = closes_from
        row.save(update_fields=["effective_to"])
        report.closed_periods.append(f"{spec.model._meta.db_table}: {row} now ends {closes_from}")


def load_reference_file(
    path: str | Path,
    *,
    loaded_by=None,
    supersede: str = "",
    reason: str = "",
    supersede_verified: bool = False,
) -> LoadReport:
    """Read a JSON fixture from disk and load it."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReferenceDataLoadError(f"{path} is not valid JSON: {exc}") from exc
    return load_reference_data(
        document,
        loaded_by=loaded_by,
        supersede=supersede,
        reason=reason,
        supersede_verified=supersede_verified,
    )


def load_reference_directory(directory: str | Path | None = None, *, loaded_by=None):
    """Load every fixture in ``reference/``, in dependency order, one file at a time.

    Deliberately NOT one transaction across all five. Each file is its own reference
    data version and each is verified separately, so a later file failing must not
    roll back an earlier one that loaded cleanly - the person fixing the failure
    should not also have to reload what already worked.

    Files already loaded report as such and are skipped, so this is safe to re-run.
    """
    base = Path(directory) if directory else Path(FIXTURE_DIRECTORY)
    missing = [name for name in FIXTURE_ORDER if not (base / name).exists()]
    if missing:
        raise ReferenceDataLoadError(
            f"Missing from {base}: {', '.join(missing)}. The load order is fixed "
            f"because the rule set fixtures reference sectors the first file creates."
        )

    reports = []
    for name in FIXTURE_ORDER:
        reports.append(load_reference_file(base / name, loaded_by=loaded_by))
    return reports
