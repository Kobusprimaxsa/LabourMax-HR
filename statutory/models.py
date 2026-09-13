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

    Contract cleaning has three, and the gazette's lettering is not the one people
    assume: **Area A is the metropolitan councils, Area B is a named list of local
    councils, and Area C is the whole of KwaZulu-Natal** (GN R.7083, GG 54075,
    3 February 2026). KwaZulu-Natal is the awkward one: the gazette gives it no
    figure at all and points instead at the collective agreement concluded in the
    Bargaining Council for the Contract Cleaning Service Industry. That is why
    ``uses_bargaining_council_rates`` exists as data — the loader needs to know a
    different source applies, and the citation on those rows is a clause of an
    agreement rather than a gazette number.
    """

    sector = models.ForeignKey(Sector, on_delete=models.PROTECT, related_name="areas")
    code = models.CharField(max_length=20, help_text="AREA_A | AREA_B | AREA_C_KZN")
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


# ---------------------------------------------------------------------- PAYE


class TaxYear(AuditedModel, AuditMixin):
    """A SARS tax year: 1 March to the end of February.

    Anchors every PAYE table and every year-to-date accumulator. Not
    effective-dated, because a tax year IS a period — adding effective dates on top
    would give two competing notions of "when".

    ``is_open`` closes once IRP5 submission is finalised. A closed year still
    produces reprints; it stops accepting new payroll runs.
    """

    label = models.CharField(max_length=12, unique=True, help_text="e.g. '2026/2027'.")
    start_date = models.DateField(unique=True, help_text="1 March.")
    end_date = models.DateField(help_text="End of February.")
    is_open = models.BooleanField(
        default=True, help_text="Closed once IRP5 submission for the year is finalised."
    )

    class Meta:
        db_table = "tax_year"
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="tax_year_end_after_start",
            ),
        ]

    def __str__(self):
        return self.label

    @classmethod
    def for_date(cls, on_date) -> TaxYear | None:
        return cls.objects.filter(start_date__lte=on_date, end_date__gte=on_date).first()


class PayeTaxBracket(AuditedModel, AuditMixin, CitedStatutoryModel):
    """One annual PAYE bracket. Cumulative base plus a marginal rate on the excess.

    SARS publishes the tables in exactly this shape — "R X of taxable income above
    R Y, plus R Z" — and storing them as published means the calculator is a
    transcription of the table rather than an interpretation of it. Anything
    cleverer becomes impossible to reconcile against the published document.

    Bounds are **inclusive both ends**, which is how SARS prints them, and differs
    deliberately from the half-open convention used for effective dates. Getting
    this backwards puts one rand of income in two brackets, or none.
    """

    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="brackets")
    bracket_order = models.SmallIntegerField(help_text="1..n, lowest band first.")
    income_from = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="Inclusive lower bound of annual taxable income."
    )
    income_to = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Inclusive upper bound. NULL = the top bracket.",
    )
    base_tax = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="Cumulative tax at income_from, as printed by SARS.",
    )
    marginal_rate_pct = models.DecimalField(
        max_digits=6, decimal_places=3, help_text="Percent, e.g. 26.000."
    )

    class Meta:
        db_table = "paye_tax_bracket"
        ordering = ["tax_year_id", "bracket_order"]
        indexes = [models.Index(fields=["tax_year", "income_from"])]
        constraints = [
            models.UniqueConstraint(
                fields=["tax_year", "bracket_order"], name="uniq_bracket_order_per_tax_year"
            ),
            models.CheckConstraint(
                condition=models.Q(income_to__isnull=True)
                | models.Q(income_to__gt=models.F("income_from")),
                name="paye_bracket_income_range_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(marginal_rate_pct__gte=0, marginal_rate_pct__lte=100),
                name="paye_bracket_marginal_rate_is_a_percentage",
            ),
            models.CheckConstraint(
                condition=models.Q(income_from__gte=0, base_tax__gte=0),
                name="paye_bracket_amounts_not_negative",
            ),
            source_reference_not_blank("paye_tax_bracket"),
        ]

    def __str__(self):
        return f"{self.tax_year_id} band {self.bracket_order} @ {self.marginal_rate_pct}%"


class PayeRebate(AuditedModel, AuditMixin, CitedStatutoryModel):
    """Primary, secondary and tertiary rebates with their matching tax thresholds.

    ``annual_amount`` is the rebate for that tier alone. The calculator sums every
    tier the employee qualifies for by age — a 68-year-old gets primary plus
    secondary. Storing a pre-summed figure would be one number to get wrong on
    somebody's birthday.

    ``tax_threshold_annual`` is published by SARS alongside the rebates and stored
    rather than derived, for the same reason the gazetted wage figures are: the
    published number is what an employee will compare against.
    """

    class RebateType(models.TextChoices):
        PRIMARY = "primary", "Primary"
        SECONDARY = "secondary", "Secondary (65 and over)"
        TERTIARY = "tertiary", "Tertiary (75 and over)"

    tax_year = models.ForeignKey(TaxYear, on_delete=models.PROTECT, related_name="rebates")
    rebate_type = models.CharField(max_length=20, choices=RebateType.choices)
    min_age = models.SmallIntegerField(default=0, help_text="0, 65 or 75.")
    annual_amount = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="This tier only. The calculator sums the tiers."
    )
    tax_threshold_annual = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="Annual income below which no PAYE is due."
    )

    class Meta:
        db_table = "paye_rebate"
        ordering = ["tax_year_id", "min_age"]
        constraints = [
            models.UniqueConstraint(
                fields=["tax_year", "rebate_type"], name="uniq_rebate_type_per_tax_year"
            ),
            models.CheckConstraint(
                condition=models.Q(annual_amount__gte=0, tax_threshold_annual__gte=0),
                name="paye_rebate_amounts_not_negative",
            ),
            source_reference_not_blank("paye_rebate"),
        ]

    def __str__(self):
        return f"{self.tax_year_id} {self.rebate_type}"


class MedicalTaxCreditRate(AuditedModel, AuditMixin, CitedStatutoryModel):
    """Monthly medical scheme fees tax credits.

    Rare in domestic and contract cleaning employment, but a PAYE engine that
    cannot handle it is not a compliant PAYE engine, and discovering that during a
    SARS audit is worse than carrying one small table.
    """

    tax_year = models.OneToOneField(
        TaxYear, on_delete=models.PROTECT, related_name="medical_tax_credit"
    )
    main_member_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    first_dependant_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    additional_dependant_monthly = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "medical_tax_credit_rate"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    main_member_monthly__gte=0,
                    first_dependant_monthly__gte=0,
                    additional_dependant_monthly__gte=0,
                ),
                name="medical_credit_amounts_not_negative",
            ),
            source_reference_not_blank("medical_tax_credit_rate"),
        ]

    def __str__(self):
        return f"Medical credits {self.tax_year_id}"


class StatutoryParameter(AuditedModel, AuditMixin, EffectiveDatedModel, CitedStatutoryModel):
    """Every scalar statutory figure that is not a wage or a tax bracket.

    One table rather than a column per figure, because these move on different
    calendars: the UIF ceiling on ministerial notice, the BCEA threshold usually in
    April, SDL and COIDA on their own schedules. A column per figure would mean a
    migration every time one of them changed, and the entire point of this phase is
    that a rate change is a data load.

    ``value_numeric`` is NUMERIC(16,6), never a float. A rate stored as 0.01 in
    floating point is not 0.01, and UIF at 1% of a ceiling is a figure employees
    check by hand.

    ``notes`` matters more here than anywhere else in the schema. The SDL
    R500,000 exemption is **forward-looking** — it asks whether the employer
    reasonably believes payroll will exceed it over the next twelve months, which
    cannot be computed from history and is therefore a human-set flag, not a
    calculation. Recording that alongside the number is what stops someone
    "improving" it into a query over last year's payroll.
    """

    class Unit(models.TextChoices):
        ZAR = "ZAR", "Rand"
        PERCENT = "percent", "Percent"
        DAYS = "days", "Days"
        HOURS = "hours", "Hours"
        RATIO = "ratio", "Ratio"
        WEEKS = "weeks", "Weeks"

    parameter_code = models.CharField(
        max_length=60,
        db_index=True,
        help_text=(
            "UIF_EMPLOYEE_RATE_PCT, UIF_MONTHLY_CEILING, SDL_RATE_PCT, "
            "SDL_ANNUAL_EXEMPTION, COIDA_ANNUAL_CEILING, BCEA_EARNINGS_THRESHOLD, "
            "VAT_RATE_PCT, LEAVE_ACCRUAL_DIVISOR_DAYS"
        ),
    )
    value_numeric = models.DecimalField(
        max_digits=16, decimal_places=6, null=True, blank=True, help_text="Never a float."
    )
    value_text = models.CharField(
        max_length=200, blank=True, help_text="For the rare non-numeric parameter."
    )
    unit = models.CharField(max_length=20, choices=Unit.choices, default=Unit.ZAR)

    class Meta:
        db_table = "statutory_parameter"
        ordering = ["parameter_code", "-effective_from"]
        indexes = [models.Index(fields=["parameter_code", "-effective_from"])]
        constraints = [
            models.UniqueConstraint(
                fields=["parameter_code", "effective_from"],
                name="uniq_statutory_parameter_per_date",
            ),
            models.CheckConstraint(
                condition=models.Q(value_numeric__isnull=False) | ~models.Q(value_text=""),
                name="statutory_parameter_has_a_value",
            ),
            effective_range_ordered("statutory_parameter"),
            source_reference_not_blank("statutory_parameter"),
            ExclusionConstraint(
                name="statutory_parameter_no_overlapping_periods",
                expressions=[
                    (
                        DateRange("effective_from", "effective_to", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                    ("parameter_code", RangeOperators.EQUAL),
                ],
            ),
        ]

    def __str__(self):
        return f"{self.parameter_code} = {self.value_numeric or self.value_text}"


# ------------------------------------------------------------------ rule sets
#
# These three tables are what the leave, working-time and termination engines read
# instead of carrying BCEA numbers in code. One row per sector per period, so the
# 2026 reading of SD7 is still available when a 2026 payroll is re-run in 2030.
#
# NOTE, and it is a deliberate departure from the workbook: **none of these fields
# has a default.** The workbook specifies BCEA defaults on the columns — 15.000
# annual leave days, a 1.500 overtime multiplier, and so on. A column default is a
# statutory figure hard-coded into a migration, which CLAUDE.md forbids in its
# strongest terms, and it is worse than an ordinary hard-coded constant: it lets an
# unverified number enter the database looking exactly like a verified one, with a
# citation attached to the row that never covered it. Every value is loaded
# explicitly. They all come from one reading of one document anyway.


class SectorRuleSet(AuditedModel, AuditMixin, EffectiveDatedModel, CitedStatutoryModel):
    """Shared shape for the three rule sets: one row per sector per period.

    ``sector`` NULL means the BCEA default, which applies where a sectoral
    determination is silent. Abstract, so each rule set gets its own table.
    """

    sector = models.ForeignKey(
        "statutory.Sector",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="NULL = the BCEA default, used where a sectoral determination is silent.",
    )

    class Meta:
        abstract = True


def rule_set_no_overlap(table: str) -> ExclusionConstraint:
    """One rule set per sector per period.

    ``Coalesce(sector, 0)`` for the same reason as the wage table: the BCEA default
    rows have a NULL sector, NULL is not equal to NULL, and those rows would
    otherwise be the only ones allowed to overlap.
    """
    return ExclusionConstraint(
        name=f"{table}_no_overlapping_periods",
        expressions=[
            (
                DateRange("effective_from", "effective_to", RangeBoundary()),
                RangeOperators.OVERLAPS,
            ),
            (Coalesce("sector", Value(0)), RangeOperators.EQUAL),
        ],
    )


class LeaveRuleSet(SectorRuleSet):
    """The statutory leave parameters the leave engine reads. BCEA ss 20–27, SD7, SD1."""

    annual_leave_days_per_cycle_5day = models.DecimalField(
        max_digits=6, decimal_places=3, help_text="Working days per cycle on a 5-day week."
    )
    annual_leave_days_per_cycle_6day = models.DecimalField(max_digits=6, decimal_places=3)
    annual_accrual_days_per_month_5day = models.DecimalField(
        max_digits=6, decimal_places=3, help_text="Straight-line monthly accrual."
    )
    annual_accrual_days_per_month_6day = models.DecimalField(max_digits=6, decimal_places=3)
    annual_accrual_ratio_days_worked = models.SmallIntegerField(
        help_text="One day per N days worked — the written-agreement alternative."
    )
    annual_accrual_ratio_hours_worked = models.SmallIntegerField(
        help_text="One hour per N hours worked."
    )
    annual_leave_cycle_months = models.SmallIntegerField()
    annual_leave_forfeit_months = models.SmallIntegerField(
        help_text="Leave must be taken within N months of cycle end."
    )
    annual_leave_payable_on_termination = models.BooleanField()

    sick_leave_cycle_months = models.SmallIntegerField()
    sick_leave_weeks_equivalent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Entitlement expressed in weeks of days normally worked.",
    )
    sick_leave_first_six_months_ratio = models.SmallIntegerField(
        help_text="One day per N days worked during the first six months of employment."
    )
    sick_leave_payable_on_termination = models.BooleanField()

    family_responsibility_days = models.SmallIntegerField(help_text="Per 12-month cycle.")
    family_resp_min_service_months = models.SmallIntegerField()
    family_resp_min_days_per_week = models.SmallIntegerField()

    # Under an interim Constitutional Court reading-in: four months plus ten days,
    # shareable between parents. Held as data precisely because remedial
    # legislation is expected, and when it arrives this must be a data load rather
    # than a release.
    parental_leave_total_days = models.SmallIntegerField()
    parental_leave_shareable = models.BooleanField()
    maternity_pre_birth_reserved_days = models.SmallIntegerField(
        help_text="Days reserved to the birth mother, if any."
    )

    class Meta:
        db_table = "leave_rule_set"
        constraints = [
            effective_range_ordered("leave_rule_set"),
            source_reference_not_blank("leave_rule_set"),
            rule_set_no_overlap("leave_rule_set"),
        ]

    def __str__(self):
        return f"Leave rules {self.sector_id or 'BCEA'} from {self.effective_from}"


class WorkingTimeRuleSet(SectorRuleSet):
    """Ordinary hours, overtime and premium pay. BCEA ss 9–18, SD7, SD1."""

    ordinary_hours_per_week = models.DecimalField(max_digits=5, decimal_places=2)
    ordinary_hours_per_day_5day = models.DecimalField(max_digits=5, decimal_places=2)
    ordinary_hours_per_day_6day = models.DecimalField(max_digits=5, decimal_places=2)

    overtime_multiplier = models.DecimalField(max_digits=5, decimal_places=3)
    max_overtime_hours_per_day = models.DecimalField(max_digits=5, decimal_places=2)
    max_overtime_hours_per_week = models.DecimalField(max_digits=5, decimal_places=2)

    # Two Sunday multipliers, and the distinction is the one most often got wrong:
    # double time when Sunday is NOT an ordinary working day for that employee,
    # time and a half when it is.
    sunday_multiplier_non_ordinary = models.DecimalField(max_digits=5, decimal_places=3)
    sunday_multiplier_ordinary = models.DecimalField(max_digits=5, decimal_places=3)

    public_holiday_worked_multiplier = models.DecimalField(max_digits=5, decimal_places=3)
    public_holiday_not_worked_paid = models.BooleanField(
        help_text="Paid when the holiday falls on a day the employee would ordinarily work."
    )

    night_work_start_time = models.TimeField(help_text="BCEA s17 night-work window.")
    night_work_end_time = models.TimeField()
    night_allowance_type = models.CharField(
        max_length=20, help_text="percentage | fixed_amount | time_off"
    )
    night_allowance_value = models.DecimalField(
        max_digits=10, decimal_places=4, help_text="Percent of hourly rate, or rand per shift."
    )

    standby_allowance_per_shift = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="SD7 night standby allowance. Gazetted, escalates.",
    )
    standby_window_start = models.TimeField()
    standby_window_end = models.TimeField()
    standby_hours_before_overtime = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Actual work beyond this within a standby shift attracts overtime.",
    )

    min_paid_hours_per_day = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="SD1 guarantees paid hours if an employee is called in.",
    )
    meal_interval_after_hours = models.DecimalField(max_digits=5, decimal_places=2)
    meal_interval_minutes = models.SmallIntegerField()
    daily_rest_hours = models.SmallIntegerField()
    weekly_rest_hours = models.SmallIntegerField()
    accommodation_deduction_max_pct = models.DecimalField(
        max_digits=5, decimal_places=2, help_text="SD7 permitted accommodation deduction."
    )

    class Meta:
        db_table = "working_time_rule_set"
        constraints = [
            effective_range_ordered("working_time_rule_set"),
            source_reference_not_blank("working_time_rule_set"),
            rule_set_no_overlap("working_time_rule_set"),
            models.CheckConstraint(
                condition=models.Q(overtime_multiplier__gte=1),
                name="working_time_overtime_multiplier_at_least_one",
            ),
        ]

    def __str__(self):
        return f"Working time {self.sector_id or 'BCEA'} from {self.effective_from}"


class TerminationRuleSet(SectorRuleSet):
    """Notice, severance and pro-rata bonus. BCEA ss 37–41, SD7, SD1."""

    # SD7 and SD1 accelerate to four weeks at six months, where the BCEA default
    # would still be two. Getting this wrong underpays notice on every termination
    # in the first year of service.
    notice_weeks_under_6_months = models.DecimalField(max_digits=5, decimal_places=2)
    notice_weeks_6_months_and_over = models.DecimalField(max_digits=5, decimal_places=2)
    notice_weeks_over_1_year = models.DecimalField(max_digits=5, decimal_places=2)

    severance_weeks_per_completed_year = models.DecimalField(max_digits=5, decimal_places=2)
    severance_requires_operational_reason = models.BooleanField(
        help_text="Severance is a retrenchment entitlement, not a general one."
    )

    annual_bonus_weeks = models.DecimalField(
        max_digits=6, decimal_places=3, help_text="Contract cleaning: 4.333 weeks under SD1."
    )
    annual_bonus_month = models.SmallIntegerField(help_text="Calendar month of normal payment.")
    annual_bonus_pro_rata_on_termination = models.BooleanField()
    annual_bonus_min_service_months = models.SmallIntegerField()

    class Meta:
        db_table = "termination_rule_set"
        constraints = [
            effective_range_ordered("termination_rule_set"),
            source_reference_not_blank("termination_rule_set"),
            rule_set_no_overlap("termination_rule_set"),
            models.CheckConstraint(
                condition=models.Q(annual_bonus_month__gte=1, annual_bonus_month__lte=12),
                name="termination_annual_bonus_month_is_a_month",
            ),
        ]

    def __str__(self):
        return f"Termination rules {self.sector_id or 'BCEA'} from {self.effective_from}"


# ------------------------------------------------------------- public holidays


class PublicHoliday(AuditedModel, AuditMixin, CitedStatutoryModel):
    """South African public holidays, including shifted Sundays and proclaimed days.

    The Public Holidays Act provides that when a holiday falls on a Sunday, the
    **following Monday** becomes the public holiday. This is stored as the Monday
    row with ``shifted_from_date`` pointing at the Sunday, rather than computed at
    read time. Two reasons: the shift affects pay on a specific date and a payroll
    re-run must reproduce it exactly, and the rule has exceptions in practice —
    election days and days of mourning are proclaimed once-off and follow no rule at
    all.

    Holidays are stored per calendar date rather than as a recurring rule for the
    same reason. Easter moves, proclamations happen, and a rule engine that gets
    Good Friday wrong in one year is worse than a table someone has to extend
    annually — which the watch calendar reminds them to do.
    """

    holiday_date = models.DateField(db_index=True)
    name = models.CharField(max_length=120)
    country_code = models.CharField(max_length=2, default="ZA")
    is_statutory = models.BooleanField(
        default=True, help_text="False for once-off proclaimed days: elections, mourning."
    )
    shifted_from_date = models.DateField(
        null=True,
        blank=True,
        help_text="The Sunday this holiday moved from, where the Act's Monday rule applied.",
    )

    class Meta:
        db_table = "public_holiday"
        ordering = ["holiday_date"]
        indexes = [models.Index(fields=["holiday_date"])]
        constraints = [
            models.UniqueConstraint(
                fields=["country_code", "holiday_date"], name="uniq_public_holiday_per_date"
            ),
            models.CheckConstraint(
                condition=models.Q(shifted_from_date__isnull=True)
                | models.Q(shifted_from_date__lt=models.F("holiday_date")),
                name="public_holiday_shifted_from_is_earlier",
            ),
            source_reference_not_blank("public_holiday"),
        ]

    def __str__(self):
        return f"{self.holiday_date} {self.name}"


# ----------------------------------------------------------- SARS source codes


class SarsSourceCode(AuditedModel, AuditMixin):
    """IRP5 / IT3(a) source codes and their tax treatment.

    This table is what stops "is this taxable?" being decided in four places. Every
    payslip line carries a source code, and the four booleans here decide whether
    the amount enters the PAYE, UIF, SDL and COIDA bases. They are separate columns
    because the bases genuinely differ — **commission is excluded from the UIF
    contribution base while bonuses are not** — and a single ``is_taxable`` flag
    would quietly get that wrong.

    Validity is bounded by tax year rather than by date: SARS introduces and retires
    codes at tax-year boundaries, and a code valid in 2026/2027 must stay usable for
    reprints of that year forever.
    """

    class Group(models.TextChoices):
        INCOME = "income", "Income"
        ALLOWANCE = "allowance", "Allowance"
        FRINGE_BENEFIT = "fringe_benefit", "Fringe benefit"
        DEDUCTION = "deduction", "Deduction"
        EMPLOYER_CONTRIBUTION = "employer_contribution", "Employer contribution"
        TOTAL = "total", "Total"

    code = models.CharField(max_length=6, unique=True, help_text="e.g. 3601, 3605, 3699, 4141.")
    description = models.CharField(max_length=200)
    code_group = models.CharField(max_length=30, choices=Group.choices, db_index=True)

    is_taxable = models.BooleanField(default=True)
    is_uif_remuneration = models.BooleanField(
        default=True, help_text="Part of the UIF contribution base. Commission is NOT."
    )
    is_sdl_remuneration = models.BooleanField(default=True)
    is_coida_remuneration = models.BooleanField(default=True)

    valid_from_tax_year = models.ForeignKey(
        TaxYear, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    valid_to_tax_year = models.ForeignKey(
        TaxYear, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sars_source_code"
        ordering = ["code"]
        indexes = [models.Index(fields=["code_group"])]

    def __str__(self):
        return f"{self.code} {self.description}"


# -------------------------------------------------------------------- banking


class Bank(models.Model):
    """South African banks, for payslip display and EFT file validation.

    Not a ``CitedStatutoryModel``: a universal branch code is published by the bank,
    not gazetted, so demanding a statutory citation would invite a fabricated one.

    The length bounds feed account-number validation. Catching a wrong account
    number before the EFT file is generated is the difference between a correction
    and an employee not being paid — which for a domestic worker paid monthly is not
    a clerical matter.
    """

    name = models.CharField(max_length=120, unique=True)
    universal_branch_code = models.CharField(max_length=10, blank=True)
    bic_swift = models.CharField(max_length=11, blank=True)
    account_number_min_length = models.SmallIntegerField()
    account_number_max_length = models.SmallIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "bank"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    account_number_max_length__gte=models.F("account_number_min_length")
                ),
                name="bank_account_length_bounds_ordered",
            ),
        ]

    def __str__(self):
        return self.name


class BankBranch(models.Model):
    """Branch codes for the cases where a universal code is not used."""

    bank = models.ForeignKey(Bank, on_delete=models.PROTECT, related_name="branches")
    branch_code = models.CharField(max_length=10)
    branch_name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "bank_branch"
        ordering = ["bank_id", "branch_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["bank", "branch_code"], name="uniq_branch_code_per_bank"
            ),
        ]

    def __str__(self):
        return f"{self.branch_code} {self.branch_name}"
