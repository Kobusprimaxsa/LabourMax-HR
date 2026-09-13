"""Statutory reference data — phase P2.

Every rate, threshold, multiplier and leave rule in the product lives in one of
these tables. **Nothing statutory is ever a constant in code** (CLAUDE.md), so the
annual March change is a data load rather than a release, and a payroll run for
March 2026 still calculates identically when re-run in 2029.

This module holds the spine of the phase, because everything else depends on it:

``ReferenceDataVersion``
    What was loaded, by whom, checked by whom, and — critically —
    ``data_current_through``, the date beyond which the system admits it does not
    know the correct rates. A payroll run past that date is blocked rather than
    quietly computed against superseded figures.

``StatutoryWatchItem``
    The maintenance calendar as data instead of as somebody's memory. One row per
    figure that moves, when it is next expected, and when it was last confirmed.
    Its purpose is to prevent a missed rate change, not to detect one afterwards.

The abstract bases below are what the remaining eighteen tables inherit.
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Func, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.audit import AuditedModel
from core.models import AuditMixin

# ------------------------------------------------------------ constraint helpers
#
# These are functions rather than constraints on an abstract Meta, because Django
# does NOT merge an abstract base's ``Meta.constraints`` into a child that declares
# its own ``Meta`` — and every model here declares one, for ``db_table``. The
# constraint would simply not exist, while the base class made it look as though it
# did. That is the same shape as row-level security without FORCE, and it is caught
# by the generated test in ``statutory/tests/test_citations.py`` rather than trusted.


def source_reference_not_blank(model_name: str) -> models.CheckConstraint:
    """Every cited row must actually carry its citation.

    A ``NOT NULL`` CharField accepts the empty string, so NOT NULL alone forbids an
    uncited rate in appearance only.
    """
    return models.CheckConstraint(
        condition=~models.Q(source_reference=""),
        name=f"{model_name}_source_reference_not_blank",
    )


def effective_range_ordered(model_name: str) -> models.CheckConstraint:
    """``effective_to`` is exclusive, so it must be strictly after ``effective_from``."""
    return models.CheckConstraint(
        condition=models.Q(effective_to__isnull=True)
        | models.Q(effective_to__gt=models.F("effective_from")),
        name=f"{model_name}_effective_range_ordered",
    )


# --------------------------------------------------------------- abstract bases


class EffectiveDatedModel(models.Model):
    """A row that is true for a period, and is never edited in place.

    Invariant 2: changing a statutory value **inserts a row and closes the
    previous one**. The half-open convention is deliberate and consistent
    everywhere — ``effective_from`` inclusive, ``effective_to`` exclusive, NULL
    meaning "still in force". Mixing conventions is how a rate change lands one
    day early for one sector and one day late for another.
    """

    effective_from = models.DateField(db_index=True)
    effective_to = models.DateField(
        null=True,
        blank=True,
        help_text="Exclusive. NULL means currently in force.",
    )

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_from and self.effective_to <= self.effective_from:
            raise ValidationError({"effective_to": "The end date must be after the start date."})


class CitedStatutoryModel(models.Model):
    """A row carrying a figure that came from a published source.

    ``source_reference`` is mandatory, and a CHECK enforces that it is not blank.
    That second part is not belt-and-braces: a ``NOT NULL`` CharField in Django
    accepts the empty string happily, so NOT NULL alone would let an uncited rate
    through while looking like it forbade one — the same shape of mistake as row
    level security without FORCE.

    ``source_url`` stays optional on purpose. Older gazettes are not all online,
    and a required URL field is a field people fill with something plausible.
    A citation you can find in a library beats a URL that 404s.

    The reference is free text because the sources genuinely differ: a gazette
    notice, an Act and section, a SARS rate table, or the KwaZulu-Natal contract
    cleaning bargaining council's collective agreement, which is not gazetted at
    all.
    """

    source_reference = models.CharField(
        max_length=200,
        help_text=(
            "Where this figure comes from, precisely enough to find it again. "
            "e.g. 'GN 7083, GG 54075, 2 Feb 2026' or 'BCCCI Collective Agreement 2026, cl 8'."
        ),
    )
    source_url = models.URLField(
        max_length=400,
        blank=True,
        help_text=(
            "Optional. Not every gazette is online, and a fabricated link is worse than none."
        ),
    )
    notes = models.TextField(
        blank=True,
        help_text=(
            "Interpretation notes — how the figure was read, and anything ambiguous about it."
        ),
    )

    class Meta:
        abstract = True


# ------------------------------------------------------------ reference version


class ReferenceDataVersion(AuditedModel, AuditMixin):
    """One batch load of statutory reference data.

    A finalised payslip records which version it was computed against, so the
    question "what did we believe the UIF ceiling was in June 2027" has an answer
    that does not depend on today's data.

    There is no ``is_active`` column, deliberately. Which version is in force is
    derived — see ``in_force_on()`` — so nobody can activate an unverified
    version by flipping a boolean.
    """

    version_label = models.CharField(max_length=40, unique=True, help_text="e.g. 'REF-2026.03.01'.")
    applies_from = models.DateField(db_index=True)
    description = models.TextField(blank=True, help_text="What changed, and why.")

    loaded_at = models.DateTimeField(default=timezone.now)
    loaded_by_user = models.ForeignKey(
        "core.AppUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    checksum = models.CharField(
        max_length=64,
        blank=True,
        help_text="SHA-256 of the loaded fixture set, so a silently edited fixture is detectable.",
    )

    # Verification is a SECOND pass by a SECOND person against the source
    # document. The loader cannot verify its own load - that is the whole point
    # of the column existing separately from loaded_by_user.
    verified_by_user = models.ForeignKey(
        "core.AppUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    golden_tests_passed = models.BooleanField(
        default=False,
        help_text=(
            "The published SARS and DEL worked examples reproduce exactly against this "
            "version. A version that fails is never in force."
        ),
    )

    data_current_through = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "THE STALENESS GUARD. The last date the statutory data is confirmed correct "
            "for. A payroll run whose period ends after this is blocked rather than "
            "computed against figures nobody has checked."
        ),
    )

    class Meta:
        db_table = "reference_data_version"
        indexes = [models.Index(fields=["data_current_through"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(data_current_through__isnull=True)
                | models.Q(data_current_through__gte=models.F("applies_from")),
                name="ref_version_current_through_after_applies_from",
            ),
            # The workbook's rule: data_current_through is advanced only AFTER
            # verification, never as part of the load. Enforced rather than
            # documented, because the load is the moment someone is in a hurry.
            models.CheckConstraint(
                condition=models.Q(data_current_through__isnull=True)
                | models.Q(verified_at__isnull=False),
                name="ref_version_current_through_requires_verification",
            ),
            models.CheckConstraint(
                condition=models.Q(verified_at__isnull=True, verified_by_user__isnull=True)
                | models.Q(verified_at__isnull=False, verified_by_user__isnull=False),
                name="ref_version_verified_by_and_at_together",
            ),
        ]

    def __str__(self):
        return self.version_label

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    @property
    def is_usable(self) -> bool:
        """Verified by a second pass AND reproducing the worked examples."""
        return self.is_verified and self.golden_tests_passed

    @classmethod
    def in_force_on(cls, on_date) -> ReferenceDataVersion | None:
        """The newest usable version applying on or before ``on_date``.

        An unverified version, or one whose golden tests fail, is invisible here.
        That is why activation is derived rather than stored: there is no boolean
        to set at 17:55 on the last day of February.
        """
        return (
            cls.objects.filter(
                applies_from__lte=on_date,
                verified_at__isnull=False,
                golden_tests_passed=True,
            )
            .order_by("-applies_from", "-id")
            .first()
        )


# ---------------------------------------------------------------- watch calendar


class StatutoryWatchItem(AuditedModel, AuditMixin):
    """One row per statutory figure that moves, and when to look for it.

    Decision D-51. The reminders that fire against this are outside the product,
    but the calendar itself is data so a new person inherits it instead of
    discovering it.
    """

    class Cadence(models.TextChoices):
        ANNUAL_1_MARCH = "annual_1_march", "Annual, effective 1 March"
        ANNUAL_1_MAY = "annual_1_may", "Annual, effective 1 May"
        IRREGULAR = "irregular", "Irregular"
        AS_PROCLAIMED = "as_proclaimed", "As proclaimed"

    class Status(models.TextChoices):
        CURRENT = "current", "Current"
        DUE = "due", "Due"
        OVERDUE = "overdue", "Overdue"
        CHANGE_PUBLISHED_NOT_LOADED = "change_published_not_loaded", "Change published, not loaded"

    watch_code = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=150)
    description = models.TextField(
        blank=True, help_text="What to look for, and how to recognise that it has changed."
    )

    change_cadence = models.CharField(
        max_length=30, choices=Cadence.choices, default=Cadence.ANNUAL_1_MARCH, db_index=True
    )
    typical_publication_window = models.CharField(
        max_length=60, blank=True, help_text="e.g. 'late January to mid February'."
    )
    next_expected_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text="NULL for genuinely unpredictable items, which are swept quarterly instead.",
    )

    last_confirmed_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "When someone last checked this against the source, whether or not it had "
            "changed. A 'no change' answer still advances this — the record that the "
            "check happened matters as much as the change."
        ),
    )
    last_confirmed_by_user = models.ForeignKey(
        "core.AppUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_change_effective_date = models.DateField(null=True, blank=True)

    source_name = models.CharField(
        max_length=150, help_text="e.g. 'Department of Employment and Labour gazette'."
    )
    source_url = models.URLField(max_length=400, blank=True)

    # 30, not the workbook's 25: 'change_published_not_loaded' is 27 characters.
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.CURRENT, db_index=True
    )
    responsible_role = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "statutory_watch_item"
        indexes = [
            models.Index(fields=["next_expected_date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.watch_code} ({self.status})"

    def confirm_checked(self, *, on_date=None, user=None, changed_effective_from=None):
        """Record that someone looked, whether or not anything had changed.

        The no-change case is the one that matters. Without it there is no way to
        distinguish "the UIF ceiling did not move" from "nobody checked", and
        those two have very different consequences in March.
        """
        self.last_confirmed_date = on_date or timezone.localdate()
        self.last_confirmed_by_user = user
        if changed_effective_from is not None:
            self.last_change_effective_date = changed_effective_from
            self.status = self.Status.CHANGE_PUBLISHED_NOT_LOADED
        else:
            self.status = self.Status.CURRENT
        self.save(
            update_fields=[
                "last_confirmed_date",
                "last_confirmed_by_user",
                "last_change_effective_date",
                "status",
                "updated_at",
            ]
        )
        return self


# ------------------------------------------------------------- sectors and areas


class Sector(AuditedModel, AuditMixin):
    """A sectoral determination the product supports.

    Which sector an employer belongs to decides which rule set every calculator
    loads, so this is not a label — it is the switch. The flags are here rather
    than in code because the two sectors differ in ways that would otherwise
    become ``if sector == "CONTRACT_CLEANING"`` scattered through the engine.
    """

    class Code(models.TextChoices):
        DOMESTIC = "DOMESTIC", "Domestic worker sector"
        CONTRACT_CLEANING = "CONTRACT_CLEANING", "Contract cleaning sector"

    code = models.CharField(max_length=30, unique=True, choices=Code.choices)
    name = models.CharField(max_length=120)
    determination_reference = models.CharField(
        max_length=120,
        blank=True,
        help_text="e.g. 'Sectoral Determination 7' or 'Sectoral Determination 1'.",
    )
    uses_area_rates = models.BooleanField(
        default=False,
        help_text="True for contract cleaning, where the minimum differs by geographic area.",
    )
    has_statutory_annual_bonus = models.BooleanField(
        default=False,
        help_text="True for contract cleaning: 4.333 weeks' remuneration, accrued monthly.",
    )
    has_provident_fund = models.BooleanField(
        default=False,
        help_text="True for contract cleaning, via the bargaining council.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "sector"

    def __str__(self):
        return self.name


class SectorArea(AuditedModel, AuditMixin):
    """A geographic wage area within a sector.

    Contract cleaning has Area A, Area B (KwaZulu-Natal) and Area C. KwaZulu-Natal
    is the awkward one: its rates come from the BCCCI collective agreement rather
    than from the gazette, which is why ``uses_bargaining_council_rates`` exists as
    data — the loader needs to know a different source applies, and the citation on
    those rows will not be a gazette number.
    """

    sector = models.ForeignKey(Sector, on_delete=models.PROTECT, related_name="areas")
    code = models.CharField(max_length=20, help_text="AREA_A | AREA_B_KZN | AREA_C")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, help_text="Which councils and metros are included.")
    uses_bargaining_council_rates = models.BooleanField(
        default=False,
        help_text="True for KwaZulu-Natal: rates come from the BCCCI collective agreement.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "sector_area"
        constraints = [
            models.UniqueConstraint(fields=["sector", "code"], name="uniq_area_code_per_sector"),
        ]

    def __str__(self):
        return f"{self.sector.code} {self.code}"


class MunicipalityAreaMap(AuditedModel, AuditMixin, EffectiveDatedModel):
    """Maps a municipality to a wage area, so the area is derived, not guessed.

    Effective-dated because municipal boundaries and names change — amalgamations
    have moved workplaces between areas before. A payroll re-run for 2026 must use
    the mapping as it stood in 2026, not today's.

    Asking an employer "are you in Area A or Area C?" produces a wrong answer
    confidently. Deriving it from the workplace address produces a wrong answer
    visibly, which can be corrected.
    """

    class MunicipalityType(models.TextChoices):
        METRO = "metro", "Metropolitan municipality"
        DISTRICT = "district", "District municipality"
        LOCAL = "local", "Local municipality"

    sector_area = models.ForeignKey(
        SectorArea, on_delete=models.PROTECT, related_name="municipalities"
    )
    province_code = models.CharField(
        max_length=10, db_index=True, help_text="GP|WC|KZN|EC|FS|MP|LP|NW|NC"
    )
    municipality_name = models.CharField(max_length=120, db_index=True)
    municipality_type = models.CharField(
        max_length=20, choices=MunicipalityType.choices, default=MunicipalityType.LOCAL
    )

    class Meta:
        db_table = "municipality_area_map"
        indexes = [models.Index(fields=["province_code", "municipality_name"])]
        constraints = [
            models.UniqueConstraint(
                fields=["municipality_name", "effective_from"],
                name="uniq_municipality_mapping_per_date",
            ),
            effective_range_ordered("municipality_area_map"),
        ]

    def __str__(self):
        return f"{self.municipality_name} -> {self.sector_area_id}"


class JobGrade(AuditedModel, AuditMixin):
    """A job category within a sector that carries its own minimum rate.

    Contract cleaning: general cleaner, supervisor, window cleaner, machine
    operator. Domestic: domestic worker, gardener, child minder, home carer.
    """

    sector = models.ForeignKey(Sector, on_delete=models.PROTECT, related_name="job_grades")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    sort_order = models.SmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "job_grade"
        ordering = ["sector_id", "sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["sector", "code"], name="uniq_job_grade_per_sector"),
        ]

    def __str__(self):
        return self.name


# ------------------------------------------------------------------ minimum wages


class DateRange(Func):
    """``daterange(from, to, '[)')`` — for the overlap exclusion constraint."""

    function = "daterange"
    output_field = DateRangeField()


class MinimumWageRate(AuditedModel, AuditMixin, EffectiveDatedModel, CitedStatutoryModel):
    """The effective-dated minimum wage table. Every capture and every run validates against it.

    Four nullable scope columns, each meaning "applies to everything below me":

    - ``sector`` NULL is the general National Minimum Wage
    - ``sector_area`` NULL is a sector that does not use areas (domestic)
    - ``job_grade`` NULL applies to every grade in the sector
    - ``hours_band`` covers the SD7 split between 27-hour weeks and longer

    ``hourly_rate`` is authoritative and everything else is derived from it. The
    gazetted weekly, monthly and daily figures are stored **as published** rather
    than recomputed, because the gazettes round and a payslip that disagrees with
    the gazette by two cents is a dispute nobody wants to have. Where both exist
    and they disagree, the gazetted figure is what the employer will be shown.

    Two constraints do the real work, and both are database-level because a rate
    table with overlapping periods produces a different answer depending on row
    order, which is the least debuggable class of payroll bug:

    ``UniqueConstraint(..., nulls_distinct=False)``
        In PostgreSQL NULL is not equal to NULL, so an ordinary unique constraint
        would happily allow two National Minimum Wage rows for the same date —
        precisely the rows most likely to be loaded twice. ``nulls_distinct=False``
        (PostgreSQL 15+, Django 5.0+) makes NULLs compare equal for this purpose.

    ``ExclusionConstraint``
        Forbids two rows for the same scope whose date ranges overlap at all, not
        merely those starting on the same day. ``Coalesce(..., 0)`` on the three
        nullable keys for the same NULL-is-not-NULL reason: without it, overlapping
        National Minimum Wage periods would slip straight through.
    """

    class HoursBand(models.TextChoices):
        ALL = "all", "All hours"
        LTE_27 = "lte_27_hours", "27 ordinary hours per week or fewer"
        GT_27 = "gt_27_hours", "More than 27 ordinary hours per week"

    sector = models.ForeignKey(
        Sector,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="minimum_wages",
        help_text="NULL = the general National Minimum Wage.",
    )
    sector_area = models.ForeignKey(
        SectorArea,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="minimum_wages",
        help_text="NULL when the sector does not use area rates.",
    )
    job_grade = models.ForeignKey(
        JobGrade,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="minimum_wages",
        help_text="NULL = applies to every grade in the sector.",
    )
    hours_band = models.CharField(max_length=20, choices=HoursBand.choices, default=HoursBand.ALL)

    hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        help_text="The authoritative figure. Four decimals: gazettes publish rates like 30.2300.",
    )
    weekly_rate_45h = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="As gazetted, not recomputed. Gazettes round.",
    )
    monthly_rate_45h = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, help_text="As gazetted."
    )
    daily_rate_9h = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, help_text="As gazetted."
    )

    class Meta:
        db_table = "minimum_wage_rate"
        indexes = [models.Index(fields=["effective_from", "effective_to"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(hourly_rate__gt=0),
                name="minimum_wage_rate_hourly_rate_positive",
            ),
            effective_range_ordered("minimum_wage_rate"),
            source_reference_not_blank("minimum_wage_rate"),
            models.UniqueConstraint(
                fields=["sector", "sector_area", "job_grade", "hours_band", "effective_from"],
                name="uniq_minimum_wage_scope_per_date",
                nulls_distinct=False,
            ),
            ExclusionConstraint(
                name="minimum_wage_rate_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("effective_from", "effective_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    (Coalesce("sector", Value(0)), RangeOperators.EQUAL),
                    (Coalesce("sector_area", Value(0)), RangeOperators.EQUAL),
                    (Coalesce("job_grade", Value(0)), RangeOperators.EQUAL),
                    ("hours_band", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        scope = self.sector.code if self.sector else "NMW"
        return f"{scope} {self.hourly_rate}/h from {self.effective_from}"
