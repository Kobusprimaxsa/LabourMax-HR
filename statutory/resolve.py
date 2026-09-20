"""Effective-date resolution — the only place that asks "what applied on this date".

Every statutory table in this app is effective-dated, and every one of them is read
the same way: the row whose period contains the date, under the half-open convention
``effective_from`` inclusive and ``effective_to`` exclusive. That query is three
lines long, which is exactly why it must not be written more than once. Written by
hand in each calculator it will eventually appear as ``effective_to__gte``, which is
off by one day, in one place, for one sector, and produces a payslip that is wrong
only for employees paid on the first of the month a rate changed.

So the rule is: **a calculator never queries a statutory table.** It calls a function
here, or it is handed the resolved row. There is nothing else to import.

Two design positions worth stating, because both are easy to undo by accident:

**Missing data raises.** Not ``None``, not a default. A calculator that receives
``None`` for the UIF ceiling and carries on will produce a payslip; a calculator
that receives an exception will not. The exception is the product working correctly
— the whole staleness argument in ``staleness.py`` is the same argument one level
up. ``*_or_none`` variants exist for the screens that legitimately need to ask
whether a figure is present, and their names say what they do.

**Narrowing falls back, widening does not.** A wage lookup for a sector that has no
row for a particular job grade falls back to the sector-wide row, and a sector with
no row at all falls back to the National Minimum Wage — because the NMW genuinely is
the floor underneath every sector. The fallback order is fixed and documented below
rather than being whatever the ORM's ordering happened to return.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import Q

from statutory.models import (
    AdoptionAgeLimit,
    LeaveRuleSet,
    MedicalTaxCreditRate,
    MinimumWageRate,
    ParentalLeaveQuantum,
    PayeRebate,
    PayeTaxBracket,
    PublicHoliday,
    Sector,
    StatutoryParameter,
    TaxYear,
    TerminationNoticeBand,
    TerminationRuleSet,
    WorkingTimeRuleSet,
)


class StatutoryValueMissingError(LookupError):
    """No statutory row covers the date asked for.

    Raised rather than returned, and never caught inside a calculator. It means the
    reference data does not cover the question, which is a data problem with a
    person's name on it, not a calculation that should proceed with a guess.
    """


def in_force_on(queryset, on_date: datetime.date):
    """Filter an effective-dated queryset to the rows in force on a date.

    Half-open: ``effective_from`` inclusive, ``effective_to`` exclusive. A row ending
    on 1 March is not in force on 1 March. The exclusion constraints on these tables
    mean at most one row per scope can match, so ``.get()`` on the result is safe.
    """
    return queryset.filter(effective_from__lte=on_date).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=on_date)
    )


# ------------------------------------------------------------- scalar parameters


def parameter_or_none(
    code: str,
    on_date: datetime.date,
    *,
    sector: Sector | None = None,
    sector_area=None,
) -> StatutoryParameter | None:
    """The ``statutory_parameter`` row for a code on a date, or None.

    **Most specific first, then fall back** — the same shape ``minimum_wage()``
    uses, and for the same reason (D-236). A figure may be scoped to a sector
    and an area because the instrument that sets it is:

    1. this sector AND this area
    2. this sector, any area
    3. unscoped — what applies wherever nothing more specific does

    Every parameter loaded before the BCCCI Main Agreement is unscoped, and a
    caller that passes no scope still lands on attempt 3, so nothing that
    existed before this change behaves differently.
    """
    attempts = [
        {"sector": sector, "sector_area": sector_area},
        {"sector": sector, "sector_area": None},
        {"sector": None, "sector_area": None},
    ]
    seen = []
    for scope in attempts:
        if scope in seen:
            continue
        seen.append(scope)
        row = in_force_on(
            StatutoryParameter.objects.filter(parameter_code=code, **scope), on_date
        ).first()
        if row is not None:
            return row
    return None


def parameter(
    code: str,
    on_date: datetime.date,
    *,
    sector: Sector | None = None,
    sector_area=None,
) -> StatutoryParameter:
    """The ``statutory_parameter`` row for a code on a date. Raises if absent."""
    row = parameter_or_none(code, on_date, sector=sector, sector_area=sector_area)
    if row is None:
        raise StatutoryValueMissingError(
            f"No value for statutory parameter '{code}' on {on_date:%d %B %Y}. "
            f"The reference data does not cover this date."
        )
    return row


def parameter_value(
    code: str,
    on_date: datetime.date,
    *,
    sector: Sector | None = None,
    sector_area=None,
) -> Decimal:
    """The numeric value of a statutory parameter on a date.

    Raises if the parameter is absent, and raises again if it is present but holds
    only text — a caller asking for a number and receiving a string would otherwise
    fail much further downstream, in arithmetic, where the message says nothing
    about which figure was missing.
    """
    row = parameter(code, on_date, sector=sector, sector_area=sector_area)
    if row.value_numeric is None:
        raise StatutoryValueMissingError(
            f"Statutory parameter '{code}' on {on_date:%d %B %Y} holds text "
            f"('{row.value_text}'), not a number."
        )
    return row.value_numeric


def _refuse_bargaining_council_gap(on_date: datetime.date, *, sector, sector_area) -> None:
    """An area whose rates come from a bargaining council NEVER falls back.

    Contract cleaning Area B is all of KwaZulu-Natal, and the gazette states no
    figure for it at all: it points at the BCCCI Main Collective Agreement
    instead (D-118). So the ordinary narrowing fallback — area, then sector,
    then the National Minimum Wage — is wrong here in a way it is right
    everywhere else. The NMW is the floor under a sector the Minister has simply
    not set a rate for; it is NOT the rate for a sector whose rate is set by a
    different instrument that nobody has loaded.

    Falling back would pay a KwaZulu-Natal cleaner R30,23 where the agreement in
    force says R32,40 — a plausible figure, silently short by R2,17 an hour, and
    invisible on the payslip.

    **1–31 March 2026 was the gap this guard was written for, and it is closed**
    (O-30, closed by D-259). The 2026 agreement's clause 2(1)(a) says "the
    parties agree that the current Main Agreement shall continue to be enforced"
    and clause 2(3) carries prevailing terms forward until replacement, so a
    predecessor governed that month; it was found, transcribed and loaded, and
    March 2026 now resolves R30,86.

    The guard itself stays exactly as it was, because what it refuses is not
    "March 2026" but "an area that takes its rates from a collective agreement,
    on a date no agreement is loaded for". Two agreements now cover 1 April 2023
    onward; a date before that still refuses, and so would a date after the 2026
    agreement is replaced by one nobody has loaded.
    """
    if sector_area is None or not getattr(sector_area, "uses_bargaining_council_rates", False):
        return
    if in_force_on(
        MinimumWageRate.objects.filter(sector=sector, sector_area=sector_area), on_date
    ).exists():
        return

    raise StatutoryValueMissingError(
        f"No bargaining council wage rate is loaded for "
        f"{sector.code if sector else 'this sector'} {sector_area.code} on "
        f"{on_date:%d %B %Y}. This area takes its rates from a collective agreement, "
        f"not from the sectoral determination, so there is nothing to fall back to: "
        f"the National Minimum Wage is the floor under a sector the Minister has not "
        f"set a rate for, not the rate for one set by an instrument nobody has loaded. "
        f"Two BCCCI agreements are loaded and between them they cover 1 April 2023 "
        f"onward - Notice 1726 of 2023 in GG 48356 up to 31 March 2026, and GN R.7296 "
        f"in GG 54412 from 1 April 2026. This date falls outside both, so the "
        f"agreement that governed it has not been found and loaded. Find it before "
        f"running payroll for this period; do not infer its rates from the increase "
        f"pattern of the ones that are loaded."
    )


def minimum_wage(
    on_date: datetime.date,
    *,
    sector: Sector | None = None,
    sector_area=None,
    job_grade=None,
    hours_band: str = MinimumWageRate.HoursBand.ALL,
) -> MinimumWageRate:
    """The minimum wage row governing a scope on a date.

    Tried most specific first, dropping one dimension at a time:

    1. sector + area + grade + band
    2. sector + area + grade, band ``all``
    3. sector + area, no grade
    4. sector only
    5. the National Minimum Wage (no sector at all)

    Grade is dropped before area because a sector that uses areas — contract
    cleaning — always has an area rate, while grades are optional within it. Band
    is dropped first because ``all`` is what a sector without a split publishes.

    The National Minimum Wage is the last fallback rather than an error: it is the
    statutory floor under every sector, so a sector with no gazetted rate of its own
    is not un-priced, it is priced at the NMW.
    """
    _refuse_bargaining_council_gap(on_date, sector=sector, sector_area=sector_area)

    attempts = [
        {
            "sector": sector,
            "sector_area": sector_area,
            "job_grade": job_grade,
            "hours_band": hours_band,
        },
        {
            "sector": sector,
            "sector_area": sector_area,
            "job_grade": job_grade,
            "hours_band": MinimumWageRate.HoursBand.ALL,
        },
        {
            "sector": sector,
            "sector_area": sector_area,
            "job_grade": None,
            "hours_band": hours_band,
        },
        {
            "sector": sector,
            "sector_area": sector_area,
            "job_grade": None,
            "hours_band": MinimumWageRate.HoursBand.ALL,
        },
        {"sector": sector, "sector_area": None, "job_grade": None, "hours_band": hours_band},
        {
            "sector": sector,
            "sector_area": None,
            "job_grade": None,
            "hours_band": MinimumWageRate.HoursBand.ALL,
        },
        {
            "sector": None,
            "sector_area": None,
            "job_grade": None,
            "hours_band": MinimumWageRate.HoursBand.ALL,
        },
    ]

    seen = []
    for scope in attempts:
        if scope in seen:
            continue
        seen.append(scope)
        row = in_force_on(MinimumWageRate.objects.filter(**scope), on_date).first()
        if row is not None:
            return row

    raise StatutoryValueMissingError(
        f"No minimum wage applies on {on_date:%d %B %Y}, not even a National Minimum "
        f"Wage row. The reference data does not cover this date."
    )


# ---------------------------------------------------------------------- rule sets


def _rule_set(model, sector: Sector | None, on_date: datetime.date, sector_area=None):
    """A sector's rule set on a date, narrowing most-specific-first.

    1. this sector AND this area — one area with its own instrument
    2. this sector, any area — the sectoral determination
    3. the BCEA default row, sector NULL

    ``sector`` NULL is the BCEA default — what applies where a sectoral
    determination is silent. Falling back to it is the whole reason the NULL-sector
    row exists, and it is the same fallback for all three rule sets, so it is
    written once.

    The area step arrived with the BCCCI Main Agreement (D-240), which binds
    contract cleaning in KwaZulu-Natal only while SD1 still governs Areas A and
    C. A caller that passes no area lands on step 2 exactly as it always did.
    """
    if sector is not None and sector_area is not None:
        row = in_force_on(
            model.objects.filter(sector=sector, sector_area=sector_area), on_date
        ).first()
        if row is not None:
            return row

    if sector is not None:
        row = in_force_on(
            model.objects.filter(sector=sector, sector_area__isnull=True), on_date
        ).first()
        if row is not None:
            return row

    row = in_force_on(model.objects.filter(sector__isnull=True), on_date).first()
    if row is None:
        scope = sector.code if sector else "the BCEA default"
        raise StatutoryValueMissingError(
            f"No {model._meta.db_table.replace('_', ' ')} for {scope} on "
            f"{on_date:%d %B %Y}, and no BCEA default either."
        )
    return row


def leave_rules(sector: Sector | None, on_date: datetime.date, sector_area=None) -> LeaveRuleSet:
    """Leave entitlement rules for a sector, and optionally an area, on a date."""
    return _rule_set(LeaveRuleSet, sector, on_date, sector_area)


def annual_leave_days(
    sector: Sector | None,
    on_date: datetime.date,
    *,
    employment_start_date: datetime.date,
    six_day_week: bool,
    sector_area=None,
) -> Decimal:
    """Annual leave days per cycle, honouring a long-service band if the
    instrument states one (D-243).

    BCCCI clause 9.1(b) is the first: 28 consecutive days for MORE THAN ten
    years' service, against clause 9.1(a)'s 21 for everyone else. Every other
    instrument loaded here states no such band, and says so explicitly through
    ``has_long_service_annual_leave`` rather than by leaving columns NULL.

    Service length is counted from ``employment_start_date`` — the CURRENT
    engagement's own start date, which the caller reads from
    ``employee_engagement`` and never from an edited first row (D-103). A
    re-hire does not carry a previous engagement's service into this band.

    Which side of the boundary the exact year count falls on is DATA, read from
    ``long_service_years_inclusive`` (D-158) — never a convention applied here.
    "More than ten years" loads FALSE, so ten years to the day is still 21 days.
    """
    rules = leave_rules(sector, on_date, sector_area)
    ordinary = (
        rules.annual_leave_days_per_cycle_6day
        if six_day_week
        else rules.annual_leave_days_per_cycle_5day
    )
    if not rules.has_long_service_annual_leave:
        return ordinary

    boundary = _boundary_date(
        employment_start_date, Decimal(rules.long_service_annual_leave_years), "years"
    )
    if rules.long_service_years_inclusive:
        reached = on_date >= boundary
    else:
        reached = on_date > boundary
    if not reached:
        return ordinary

    return (
        rules.long_service_annual_leave_days_6day
        if six_day_week
        else rules.long_service_annual_leave_days_5day
    )


def working_time_rules(
    sector: Sector | None, on_date: datetime.date, sector_area=None
) -> WorkingTimeRuleSet:
    """Ordinary hours, overtime and premium rules for a sector/area on a date."""
    return _rule_set(WorkingTimeRuleSet, sector, on_date, sector_area)


class AccommodationCapState(enum.StrEnum):
    """Three states, named. A caller that forgets one gets an AttributeError,
    not a Decimal it can quietly misread (D-198 amended)."""

    NOT_LOADED = "not_loaded"  # no rule set in force for that sector on that date
    NO_CAP = "no_cap"  # the instrument states no accommodation percentage
    CAPPED = "capped"  # the instrument states one — possibly zero


@dataclasses.dataclass(frozen=True)
class AccommodationCap:
    state: AccommodationCapState
    percentage: Decimal | None = None
    source_reference: str = ""
    detail: str = ""  # why it is NOT_LOADED, in the resolver's own words

    def exceeded_by(self, percentage: Decimal) -> bool:
        """Only a CAPPED instrument can be exceeded. NO_CAP is not a cap of zero
        — that reading is exactly what the 0.00 sentinel got wrong."""
        return self.state is AccommodationCapState.CAPPED and percentage > self.percentage


class NightAllowanceState(enum.StrEnum):
    """What the instrument in force says about BCEA s17(2)(a)'s allowance.

    Four answers, not a number, for the same reason the accommodation cap has
    three (D-198): "the instrument states none" and "the instrument states zero"
    are different facts, and a payroll engine that cannot tell them apart pays
    nothing in both cases (O-22).
    """

    NOT_LOADED = "not_loaded"  # no rule set in force for that sector on that date
    BY_AGREEMENT = "by_agreement"  # s17(2)(a) applies, the parties set the figure
    TIME_OFF = "time_off"  # discharged by reducing hours, not by paying
    PERCENTAGE = "percentage"  # a stated percentage of the hourly wage
    FIXED_AMOUNT = "fixed_amount"  # a stated rand amount per shift


@dataclasses.dataclass(frozen=True)
class NightAllowance:
    state: NightAllowanceState
    value: Decimal | None = None
    window_start: datetime.time | None = None
    window_end: datetime.time | None = None
    source_reference: str = ""
    detail: str = ""  # why it is NOT_LOADED, in the resolver's own words

    @property
    def is_payable_by_this_instrument(self) -> bool:
        """Only a stated figure is priced from reference data. BY_AGREEMENT is
        payable too — but at a figure the employer agreed, which is not here."""
        return self.state in (NightAllowanceState.PERCENTAGE, NightAllowanceState.FIXED_AMOUNT)


def night_allowance(
    sector: Sector | None, on_date: datetime.date, *, sector_area=None
) -> NightAllowance:
    """What the instrument in force says about the night work allowance.

    Returns NOT_LOADED rather than raising, the same shape as
    ``accommodation_cap()``: "no reference data" is a fourth answer the caller
    must handle, not an exception to be caught in passing.

    **``sector_area`` was missing and the answer was right by coincidence**
    (D-266). One sector can be governed by two instruments (D-240), and this
    function could not reach the narrower one — for KwaZulu-Natal contract
    cleaning it answered Sectoral Determination 1's row rather than the BCCCI
    Main Agreement's. Both say 10% of the hourly wage, in both editions of the
    agreement, so nothing was ever wrong; it would have gone wrong silently the
    first time the council moved its figure, which is precisely the March event
    this table exists for. ``working_time_rules()`` has always taken the
    argument and narrows most-specific-first, so passing it through is the
    whole fix.
    """
    try:
        rules = working_time_rules(sector, on_date, sector_area=sector_area)
    except StatutoryValueMissingError as missing:
        return NightAllowance(state=NightAllowanceState.NOT_LOADED, detail=str(missing))
    return NightAllowance(
        state=NightAllowanceState(rules.night_allowance_type),
        value=rules.night_allowance_value,
        window_start=rules.night_work_start_time,
        window_end=rules.night_work_end_time,
        source_reference=rules.source_reference,
    )


def accommodation_cap(sector: Sector | None, on_date: datetime.date) -> AccommodationCap:
    """What the instrument in force says about capping an accommodation deduction.

    Returns NOT_LOADED rather than raising: "no reference data" is a third answer
    the caller must handle, not an exception to be caught in passing.
    """
    try:
        rules = working_time_rules(sector, on_date)
    except StatutoryValueMissingError as missing:
        return AccommodationCap(state=AccommodationCapState.NOT_LOADED, detail=str(missing))
    if not rules.accommodation_deduction_capped:
        return AccommodationCap(
            state=AccommodationCapState.NO_CAP, source_reference=rules.source_reference
        )
    return AccommodationCap(
        state=AccommodationCapState.CAPPED,
        percentage=rules.accommodation_deduction_max_pct,
        source_reference=rules.source_reference,
    )


#: Quoted in the refusal when the interim quantum has lapsed, so the person
#: reading it knows which judgment ran out and on what date rather than being
#: told a row is missing.
VAN_WYK = "Van Wyk v Minister of Employment and Labour (CCT 308/23) [2025] ZACC 20"


def parental_quantum(on_date: datetime.date) -> ParentalLeaveQuantum:
    """How much parental leave the law gives on ``on_date``, in months and days.

    REFUSES when nothing is in force rather than falling back (D-203, D-101's
    shape). The interim reading-in is effective-dated to the end of the 36-month
    suspension, so this is exactly what happens on 3 October 2028 if Parliament
    has not legislated and nothing has been loaded — and the one thing it must
    not do is quietly revert a parent from four months to the pre-judgment
    s25A's ten days, on a date nobody was watching.
    """
    row = in_force_on(ParentalLeaveQuantum.objects.all(), on_date).first()
    if row is None:
        latest = ParentalLeaveQuantum.objects.order_by("-effective_to").first()
        ran_out = (
            f" The interim reading-in in {VAN_WYK} was loaded to {latest.effective_to:%d %B %Y}"
            if latest is not None and latest.effective_to
            else ""
        )
        raise StatutoryValueMissingError(
            f"No parental leave quantum is in force on {on_date:%d %B %Y}.{ran_out}: the "
            f"declarations of invalidity were suspended for 36 months from 3 October 2025, "
            f"and nothing has been loaded for the period after that. Load the remedial "
            f"legislation, or whatever the Constitutional Court ordered in its place, before "
            f"capturing parental leave for this date. This refuses rather than falling back "
            f"to the repealed s25A, which would cut a parent to ten days."
        )
    return row


def adoption_age_limit(on_date: datetime.date) -> AdoptionAgeLimit:
    """Whether adoption leave is limited by the child's age on ``on_date``.

    Unlike the quantum, this one is expected to lapse INTO a row saying there is
    no limit — the Court declared the under-two limit invalid and only suspended
    that declaration (D-203). A missing row still refuses, because "nobody has
    loaded the rule" is not the same statement as "there is no limit".
    """
    row = in_force_on(AdoptionAgeLimit.objects.all(), on_date).first()
    if row is None:
        raise StatutoryValueMissingError(
            f"No adoption age limit rule is in force on {on_date:%d %B %Y}. {VAN_WYK} retains "
            f"the 'below the age of two' limit for the suspension period and strikes it down "
            f"after; load the row that applies to this date rather than guessing which."
        )
    return row


def termination_rules(
    sector: Sector | None, on_date: datetime.date, sector_area=None
) -> TerminationRuleSet:
    """Severance and pro-rata bonus rules for a sector/area on a date."""
    return _rule_set(TerminationRuleSet, sector, on_date, sector_area)


def _boundary_date(start: datetime.date, value: Decimal, unit: str) -> datetime.date:
    """The calendar date a service-length boundary falls on, counted from
    ``start``. No day/week/month/year count is written here — the boundary's
    own (value, unit), read off the loaded row, is handed to ``relativedelta``
    exactly as stored, and it is the one that knows a calendar's arithmetic.
    """
    return start + relativedelta(**{unit: float(value)})


def _band_contains(
    band: TerminationNoticeBand, start: datetime.date, on_date: datetime.date
) -> bool:
    """Whether ``on_date`` falls inside ``band``'s own service-length range,
    honouring that band's own stated inclusivity at each end (D-158,
    corrected) — never a fixed inclusive/exclusive convention applied to
    every boundary alike, because the statutes do not use one.
    """
    from_date = _boundary_date(start, band.service_from_value, band.service_from_unit)
    if band.service_from_inclusive:
        if on_date < from_date:
            return False
    elif on_date <= from_date:
        return False

    if band.service_to_value is None:
        return True

    to_date = _boundary_date(start, band.service_to_value, band.service_to_unit)
    if band.service_to_inclusive:
        return on_date <= to_date
    return on_date < to_date


def notice_band(
    sector: Sector | None,
    on_date: datetime.date,
    *,
    employment_start_date: datetime.date,
    sector_area=None,
) -> TerminationNoticeBand:
    """The one ``termination_notice_band`` covering this employee's service
    length, for a sector, as at a date (D-68).

    Computes nothing about what the boundaries themselves are — those are
    read off the rule set in force on ``on_date`` (falling back to the BCEA
    default the same way every other rule set does), and each band's own
    ``service_from``/``service_to`` are converted to calendar dates from
    ``employment_start_date`` before being compared against ``on_date``.

    Which side of an exact boundary a service length falls on is DATA, read
    per band from ``service_from_inclusive``/``service_to_inclusive`` (D-158,
    corrected) — not a rule this function applies uniformly. Trusts that
    ``statutory/checks.py::check_notice_bands()`` has already proved the
    loaded bands touch with no gap or overlap; it does not re-verify that
    here, so an inconsistent load could in principle match zero or more than
    one band. Zero is still caught, below.
    """
    rule_set = termination_rules(sector, on_date, sector_area)
    bands = list(rule_set.notice_bands.all())
    if not bands:
        scope = sector.code if sector else "the BCEA default"
        raise StatutoryValueMissingError(
            f"No termination notice bands for {scope} on {on_date:%d %B %Y}. The "
            f"reference data does not cover this."
        )

    for band in bands:
        if not _band_contains(band, employment_start_date, on_date):
            continue
        if band.is_contested:
            # D-241. Two limbs, two answers, and NOTICE IS SYMMETRIC — SD1
            # clause 23(1)(c) forbids an employee owing more notice than the
            # employer does — so taking the longer as "safer" holds a resigning
            # employee in employment longer than the law permits, and taking the
            # shorter short-changes a dismissed one. There is no safe direction
            # to guess in, which is exactly why this refuses (D-158's lesson).
            raise StatutoryValueMissingError(
                f"Notice cannot be resolved for an employee who started "
                f"{employment_start_date:%d %B %Y}, as at {on_date:%d %B %Y}. The "
                f"instrument gives two irreconcilable answers for this band and nothing "
                f"in it resolves them. {band.contested_reason} Cited to "
                f"{band.source_reference}. Neither limb may be preferred: notice is "
                f"symmetric, so over-stating it holds a resigning employee longer than "
                f"the law allows and under-stating it short-changes a dismissed one."
            )
        return band

    raise StatutoryValueMissingError(
        f"No notice band covers an employee who started {employment_start_date:%d %B %Y}, "
        f"as at {on_date:%d %B %Y}. Every rule set's bands must start at zero service."
    )


# ----------------------------------------------------------------- public holidays


def public_holiday_on(a_date: datetime.date, *, country_code: str = "ZA") -> PublicHoliday | None:
    """The public holiday falling on a date, or None.

    Returns the row rather than a boolean because callers need its name for the
    payslip line and its ``is_statutory`` flag to tell a gazetted holiday from a
    proclaimed once-off day.
    """
    return PublicHoliday.objects.filter(country_code=country_code, holiday_date=a_date).first()


def is_public_holiday(a_date: datetime.date, *, country_code: str = "ZA") -> bool:
    """Whether a date is a public holiday.

    Reads the stored row, never a recurrence rule: Easter moves, the Sunday shift is
    stored as the Monday it landed on, and proclaimed days follow no rule at all.
    """
    return public_holiday_on(a_date, country_code=country_code) is not None


def public_holidays_between(start: datetime.date, end: datetime.date, *, country_code: str = "ZA"):
    """Every public holiday from ``start`` to ``end``, both inclusive.

    Inclusive at both ends because callers pass a pay period, and a pay period's end
    date is a day people work.
    """
    return PublicHoliday.objects.filter(
        country_code=country_code, holiday_date__gte=start, holiday_date__lte=end
    ).order_by("holiday_date")


# ------------------------------------------------------------------------- PAYE


def tax_year(on_date: datetime.date) -> TaxYear:
    """The SARS tax year containing a date. Raises if none is loaded."""
    year = TaxYear.for_date(on_date)
    if year is None:
        raise StatutoryValueMissingError(
            f"No tax year covers {on_date:%d %B %Y}. The reference data does not cover this date."
        )
    return year


def paye_brackets(year: TaxYear) -> list[PayeTaxBracket]:
    """A tax year's brackets, lowest band first. Raises if the year has none."""
    brackets = list(year.brackets.order_by("bracket_order"))
    if not brackets:
        raise StatutoryValueMissingError(f"Tax year {year.label} has no PAYE brackets loaded.")
    return brackets


def paye_rebates(year: TaxYear, age: int) -> list[PayeRebate]:
    """Every rebate tier an employee of this age qualifies for, primary first.

    A list, not a sum. The calculator adds them, and it does so on the tiers that
    were actually loaded for that year: a 68-year-old qualifies for primary and
    secondary, and a stored pre-summed figure would be one more number to get wrong
    on somebody's birthday.
    """
    return list(year.rebates.filter(min_age__lte=age).order_by("min_age"))


def medical_tax_credit(year: TaxYear) -> MedicalTaxCreditRate | None:
    """A tax year's medical scheme fees credits, or None if none are loaded.

    The only PAYE lookup that legitimately returns None: an employee with no medical
    scheme has no credit, and that is the ordinary case in both of our sectors.
    """
    return MedicalTaxCreditRate.objects.filter(tax_year=year).first()
