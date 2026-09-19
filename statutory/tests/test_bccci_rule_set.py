"""Area B's rule set, and the three things BCEA s49 keeps out of it.

The BCCCI Main Agreement binds contract cleaning in KwaZulu-Natal ONLY, while
Sectoral Determination 1 still governs Areas A and C of the same sector — which
is why rule sets are scoped on area (D-240) and why every test here checks both
sides of that line.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import resolve
from statutory.loader import load_reference_data
from statutory.models import Sector, SectorArea

pytestmark = [pytest.mark.django_db, pytest.mark.statutory]

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"
ON = datetime.date(2026, 6, 1)


@pytest.fixture
def areas(db):
    sector = Sector.objects.create(
        code=Sector.Code.CONTRACT_CLEANING, name="Contract cleaning sector"
    )
    Sector.objects.create(code=Sector.Code.DOMESTIC, name="Domestic worker sector")
    made = {
        code: SectorArea.objects.create(
            sector=sector,
            code=code,
            name=code,
            uses_bargaining_council_rates=(code == "AREA_B"),
        )
        for code in ("AREA_A", "AREA_B", "AREA_C")
    }
    return sector, made


@pytest.fixture
def loaded(areas):
    """SD1's sector-wide rule sets and bands, then the BCCCI's Area B ones."""
    for name in (
        "ref-2026.03.01-leave.json",
        "ref-2026.03.01-rules.json",
        "ref-2026.03.01-sd1.json",
        "ref-2026.03.01-notice-bands.json",
        "ref-2026.04.01-bccci-rules.json",
        "ref-2026.04.01-bccci-notice.json",
    ):
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))
    return areas


# ------------------------------------ D-240: one sector, two instruments


def test_area_b_and_area_a_resolve_to_different_rule_sets(loaded):
    """The whole reason rule sets grew an area column. Loading the agreement
    over the sector would have applied KwaZulu-Natal's terms to Cape Town."""
    sector, made = loaded

    area_b = resolve.working_time_rules(sector, ON, made["AREA_B"])
    area_a = resolve.working_time_rules(sector, ON, made["AREA_A"])

    assert area_b.pk != area_a.pk
    assert area_b.sector_area_id == made["AREA_B"].pk
    assert area_a.sector_area_id is None, "SD1's row is sector-wide"
    assert "GN R.7296" in area_b.source_reference
    assert "Sectoral Determination 1" in area_a.source_reference


def test_a_caller_that_passes_no_area_still_gets_the_sector_wide_row(loaded):
    """Every caller written before the area column existed."""
    sector, made = loaded

    assert resolve.working_time_rules(sector, ON).sector_area_id is None


# -------------------------- B1b: pricing needs nothing new for Area B


def test_area_b_prices_public_holidays_and_sundays_exactly_as_the_bcea_does(loaded):
    """Clause 11 reproduces BCEA s16 and s18 almost verbatim — including
    11.1(b)(ii)'s "whichever is the greater" and 11.2(b)'s daily-wage floor on a
    short Sunday — which is precisely what ``calculators/gross.py`` already
    implements under D-217.

    So this asserts the MULTIPLIERS Area B resolves to are the same ones Area A
    resolves to, which is what makes "write no new pricing" true rather than
    hopeful. The comparison rules themselves are already golden-tested against
    the Act in ``calculators/tests/test_gross_golden.py``.
    """
    sector, made = loaded

    area_b = resolve.working_time_rules(sector, ON, made["AREA_B"])
    area_a = resolve.working_time_rules(sector, ON, made["AREA_A"])

    for field in (
        "overtime_multiplier",
        "sunday_multiplier_ordinary",
        "sunday_multiplier_non_ordinary",
        "public_holiday_worked_multiplier",
        "public_holiday_not_worked_paid",
    ):
        assert getattr(area_b, field) == getattr(area_a, field), field

    assert area_b.public_holiday_worked_multiplier == Decimal("2.000")
    assert area_b.sunday_multiplier_non_ordinary == Decimal("2.000")
    assert area_b.sunday_multiplier_ordinary == Decimal("1.500")


def test_area_b_keeps_sd1s_six_hour_minimum_and_ten_per_cent_night_allowance(loaded):
    """cl 5 and cl 4.3 — the same two figures SD1 carries, from a different
    instrument, so the citation moves and the numbers do not."""
    sector, made = loaded
    area_b = resolve.working_time_rules(sector, ON, made["AREA_B"])

    assert area_b.min_paid_hours_per_day == Decimal("6.00")
    assert area_b.night_allowance_type == "percentage"
    assert area_b.night_allowance_value == Decimal("10.0000")


def test_the_bccci_bonus_multiplier_is_four_point_three_three(loaded):
    """cl 4.5(a). SD1's own is 4.333 — a different figure in a different
    instrument that looks almost identical."""
    sector, made = loaded

    area_b = resolve.termination_rules(sector, ON, made["AREA_B"])
    area_a = resolve.termination_rules(sector, ON, made["AREA_A"])

    assert area_b.annual_bonus_weeks == Decimal("4.330")
    assert area_a.annual_bonus_weeks == Decimal("4.333")
    assert area_b.annual_bonus_weeks != area_a.annual_bonus_weeks


# --------------------- B2c: s49(1)(e) keeps the one-day certificate rule out


def test_a_kzn_employee_absent_two_consecutive_days_needs_no_certificate(loaded):
    """cl 10.3(a)(i) demands a certificate after "more than ONE consecutive day"
    where BCEA s23(1) says more than two. **BCEA s49(1)(e) forbids a bargaining
    council agreement from reducing the s22-to-s24 entitlement**, and requiring
    proof sooner than the Act allows lets pay be withheld where s23 says it may
    not be — so the clause is void to that extent and is NOT loaded.

    CORRECTED in chunk C against the gazette (D-248): cl 10.2(a) does NOT track
    s23(1) and the agreement does NOT contradict itself here. 10.2(a) states the
    same ONE-day threshold, and drops a word on top of it — "An employer is
    required to pay ... if the employee has been absent from work for one day",
    where s23(1) says an employer is NOT required to pay. So the reduction rests
    on one ground, not two, and that ground is enough.

    The threshold therefore stays at 2 for Area B, and a KwaZulu-Natal employee
    absent for exactly two consecutive days is not required to produce one.
    """
    sector, made = loaded

    threshold = resolve.parameter_value(
        "SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS", ON, sector=sector, sector_area=made["AREA_B"]
    )

    assert threshold == Decimal("2.000000"), (
        "the BCCCI's one-day rule is void under s49(1)(e) and must never be loaded as "
        "a scoped override"
    )
    assert Decimal("2") <= threshold, "two consecutive days is within the threshold"


def test_no_scoped_certificate_override_exists_for_area_b(loaded):
    """Stated as its own guard: if someone later loads cl 10.3(a)(i) as a scoped
    parameter, this fails and says why."""
    from statutory.models import StatutoryParameter

    assert not StatutoryParameter.objects.filter(
        parameter_code="SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS", sector__isnull=False
    ).exists(), "s49(1)(e) forbids reducing the s23 protection; do not scope this figure"


# ------------------------------- B2d: notice, and the window that refuses


def test_the_first_four_weeks_are_one_working_day(loaded):
    """cl 21.1(b)(i), the only unambiguous limb. One WORKING day, so the unit is
    days — contract cleaning runs six-day weeks and a day is worth a sixth of
    one, not a fifth (D-68)."""
    sector, made = loaded

    band = resolve.notice_band(
        sector,
        datetime.date(2026, 4, 14),
        employment_start_date=datetime.date(2026, 4, 1),
        sector_area=made["AREA_B"],
    )

    assert band.notice_value == Decimal("1.00")
    assert band.notice_unit == "days"
    assert band.is_contested is False


def test_the_four_weeks_to_six_months_window_refuses_and_quotes_both_limbs(loaded):
    """Two rules, two answers, and nothing in the instrument resolves them.
    Taking the longer as "safer" is not available: notice is SYMMETRIC, so
    over-stating it holds a resigning employee longer than the law allows
    (D-158)."""
    sector, made = loaded

    with pytest.raises(resolve.StatutoryValueMissingError) as raised:
        resolve.notice_band(
            sector,
            datetime.date(2026, 7, 1),
            employment_start_date=datetime.date(2026, 4, 1),
            sector_area=made["AREA_B"],
        )

    message = str(raised.value)
    assert "two irreconcilable answers" in message
    assert "Not less than two weeks notice" in message
    assert "Not less than one weeks notice" in message
    assert "no payment in lieu for the one-week probation notice" in message
    assert "notice is symmetric" in message


def test_after_six_months_the_two_week_rule_is_unambiguous(loaded):
    """The one-week rule is expressly limited to "between 4 weeks ... and six
    months" and probation is capped at four months, so only the two-week rule
    can reach an employee of longer service."""
    sector, made = loaded

    band = resolve.notice_band(
        sector,
        datetime.date(2027, 1, 5),
        employment_start_date=datetime.date(2026, 4, 1),
        sector_area=made["AREA_B"],
    )

    assert band.notice_value == Decimal("2.00")
    assert band.notice_unit == "weeks"


def test_area_b_and_area_a_do_not_share_a_notice_rule(loaded):
    """Recorded because it is easy to assume one sector has one notice regime.
    At three months' service SD1 gives four weeks and the BCCCI refuses."""
    sector, made = loaded
    on, started = datetime.date(2026, 7, 1), datetime.date(2026, 4, 1)

    area_a = resolve.notice_band(
        sector, on, employment_start_date=started, sector_area=made["AREA_A"]
    )
    assert area_a.notice_value == Decimal("4.00")
    assert area_a.notice_unit == "weeks"

    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.notice_band(sector, on, employment_start_date=started, sector_area=made["AREA_B"])


def test_a_contested_band_may_not_carry_a_period(loaded):
    """The CHECK pair, watched refusing: a value on a contested band would be a
    period nobody can stand behind sitting where a gazetted one belongs."""
    from django.db import IntegrityError, transaction

    from statutory.models import TerminationNoticeBand

    sector, made = loaded
    rule_set = resolve.termination_rules(sector, ON, made["AREA_B"])

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        TerminationNoticeBand.objects.create(
            termination_rule_set=rule_set,
            sequence=99,
            source_reference="Test",
            service_from_value=Decimal("0"),
            service_from_unit="weeks",
            service_from_inclusive=True,
            notice_value=Decimal("1"),
            notice_unit="weeks",
            is_contested=True,
            contested_reason="both limbs",
        )

    assert "notice_band_contested_has_no_value_and_a_reason" in str(raised.value)


def test_a_contested_band_must_say_why(loaded):
    from django.db import IntegrityError, transaction

    from statutory.models import TerminationNoticeBand

    sector, made = loaded
    rule_set = resolve.termination_rules(sector, ON, made["AREA_B"])

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        TerminationNoticeBand.objects.create(
            termination_rule_set=rule_set,
            sequence=98,
            source_reference="Test",
            service_from_value=Decimal("0"),
            service_from_unit="weeks",
            service_from_inclusive=True,
            is_contested=True,
        )

    assert "notice_band_contested_has_no_value_and_a_reason" in str(raised.value)


def test_an_ordinary_band_must_still_state_a_period(loaded):
    """The other half of the pair: nullable does not mean optional."""
    from django.db import IntegrityError, transaction

    from statutory.models import TerminationNoticeBand

    sector, made = loaded
    rule_set = resolve.termination_rules(sector, ON, made["AREA_B"])

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        TerminationNoticeBand.objects.create(
            termination_rule_set=rule_set,
            sequence=97,
            source_reference="Test",
            service_from_value=Decimal("0"),
            service_from_unit="weeks",
            service_from_inclusive=True,
            notice_unit="weeks",
        )

    assert "notice_band_states_a_period_unless_contested" in str(raised.value)


# ------------------------- B1c: clause 9.1(b)'s 28-day long-service band


@pytest.mark.parametrize(
    ("years_in", "expected", "why"),
    [
        (0, "15.000", "a new employee gets clause 9.1(a)"),
        (10, "15.000", "TEN YEARS EXACTLY is still the lower band - 'MORE THAN ten'"),
        (11, "20.000", "eleven years is more than ten, so clause 9.1(b) applies"),
    ],
)
def test_the_long_service_band_turns_on_after_ten_years_not_at_ten(loaded, years_in, expected, why):
    """D-158, applied to a boundary that is not a notice period: which side of an
    exact boundary a service length falls on is read off the clause's own
    wording, never a convention. One day past ten years is the first day of 28."""
    sector, made = loaded
    started = datetime.date(2026, 4, 1)

    days = resolve.annual_leave_days(
        sector,
        datetime.date(2026 + years_in, 4, 1),
        employment_start_date=started,
        six_day_week=False,
        sector_area=made["AREA_B"],
    )

    assert days == Decimal(expected), why


def test_the_day_after_ten_years_is_the_first_day_of_the_longer_entitlement(loaded):
    """The boundary itself, to the day, because a year-granularity test would
    pass whichever way the comparison was written."""
    sector, made = loaded
    started = datetime.date(2026, 4, 1)

    def days_on(on_date):
        return resolve.annual_leave_days(
            sector,
            on_date,
            employment_start_date=started,
            six_day_week=False,
            sector_area=made["AREA_B"],
        )

    assert days_on(datetime.date(2036, 4, 1)) == Decimal("15.000")
    assert days_on(datetime.date(2036, 4, 2)) == Decimal("20.000")


def test_the_long_service_band_is_six_day_aware(loaded):
    """28 consecutive days is four weeks: 20 working days on a five-day week and
    24 on a six-day week, derived exactly as the 15 and the 18 are."""
    sector, made = loaded
    on, started = datetime.date(2040, 1, 1), datetime.date(2026, 4, 1)

    def days_on(six_day):
        return resolve.annual_leave_days(
            sector,
            on,
            employment_start_date=started,
            six_day_week=six_day,
            sector_area=made["AREA_B"],
        )

    assert days_on(False) == Decimal("20.000")
    assert days_on(True) == Decimal("24.000")


def test_an_instrument_with_no_long_service_band_never_reaches_one(loaded):
    """SD1, the BCEA and SD7 all state one annual leave entitlement regardless of
    service. Forty years in Area A is still 15 days, and the column says that
    explicitly rather than leaving it to a NULL nobody reads."""
    sector, made = loaded
    rules = resolve.leave_rules(sector, ON, made["AREA_A"])

    assert rules.has_long_service_annual_leave is False
    assert rules.long_service_annual_leave_years is None

    assert resolve.annual_leave_days(
        sector,
        datetime.date(2066, 1, 1),
        employment_start_date=datetime.date(2026, 4, 1),
        six_day_week=False,
        sector_area=made["AREA_A"],
    ) == Decimal("15.000")


def test_a_rule_set_claiming_a_long_service_band_must_state_all_four_figures(loaded):
    """The CHECK pair, watched refusing. A band with no year count is a rule
    nothing can resolve, sitting where a gazetted one belongs."""
    from django.db import IntegrityError, transaction

    from statutory.models import LeaveRuleSet

    sector, made = loaded
    existing = resolve.leave_rules(sector, ON, made["AREA_B"])
    values = {
        f.attname: getattr(existing, f.attname)
        for f in LeaveRuleSet._meta.local_fields
        if f.attname not in {"id", "public_uid", "created_at", "updated_at"}
    }
    values.update(
        sector_id=None,
        sector_area_id=None,
        effective_from=datetime.date(2099, 1, 1),
        effective_to=None,
        has_long_service_annual_leave=True,
        long_service_annual_leave_years=None,
    )

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        LeaveRuleSet.objects.create(**values)

    assert "leave_rule_set_long_service_band_states_its_figures" in str(raised.value)


def test_a_rule_set_with_no_long_service_band_may_not_carry_its_figures(loaded):
    """The other direction. Figures sitting behind a FALSE flag are a band
    somebody loaded and nothing will ever read."""
    from django.db import IntegrityError, transaction

    from statutory.models import LeaveRuleSet

    sector, made = loaded
    existing = resolve.leave_rules(sector, ON, made["AREA_B"])
    values = {
        f.attname: getattr(existing, f.attname)
        for f in LeaveRuleSet._meta.local_fields
        if f.attname not in {"id", "public_uid", "created_at", "updated_at"}
    }
    values.update(
        sector_id=None,
        sector_area_id=None,
        effective_from=datetime.date(2099, 1, 1),
        effective_to=None,
        has_long_service_annual_leave=False,
    )

    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        LeaveRuleSet.objects.create(**values)

    assert "leave_rule_set_no_long_service_band_states_no_figures" in str(raised.value)


# ------------- B2b: the two additive maternity benefits s49 does not reach


@pytest.fixture
def maternity(loaded):
    load_reference_data(
        json.loads((REFERENCE / "ref-2026.04.01-bccci-maternity.json").read_text(encoding="utf-8"))
    )
    return loaded


def test_the_prenatal_clinic_benefit_is_one_paid_day_in_each_of_three_months(maternity):
    """cl 13.2. ADDITIVE, so BCEA s49 does not reach it: it takes nothing from
    anyone, which is what separates it from cl 13.3's void twelve-week cap."""
    sector, made = maternity

    def value(code):
        return resolve.parameter_value(code, ON, sector=sector, sector_area=made["AREA_B"])

    assert value("PRENATAL_CLINIC_PAID_DAYS_PER_MONTH") == Decimal("1.000000")
    assert value("PRENATAL_CLINIC_MONTHS_BEFORE_BIRTH") == Decimal("3.000000")


def test_the_return_payment_is_stored_as_a_divisor_and_never_as_a_third(maternity):
    """cl 13.4(a) says ONE THIRD of a month's wage. 0.333333 is a rounded figure
    standing where an exact one belongs, and this is money: a third of R9 000 is
    R3 000 exactly, where 0,333333 x 9000 is R2 999,997. The divisor divides."""
    sector, made = maternity

    divisor = resolve.parameter_value(
        "MATERNITY_RETURN_PAYMENT_MONTH_DIVISOR", ON, sector=sector, sector_area=made["AREA_B"]
    )

    assert divisor == Decimal("3.000000")
    assert Decimal("9000") / divisor == Decimal("3000")


def test_neither_additive_benefit_reaches_a_contract_cleaner_outside_kwazulu_natal(maternity):
    """The agreement binds Area B only. There is no unscoped row to fall back
    to, so an Area A employer asking for it is refused rather than given a
    KwaZulu-Natal benefit their instrument never granted."""
    sector, made = maternity

    for code in (
        "PRENATAL_CLINIC_PAID_DAYS_PER_MONTH",
        "PRENATAL_CLINIC_MONTHS_BEFORE_BIRTH",
        "MATERNITY_RETURN_PAYMENT_MONTH_DIVISOR",
    ):
        with pytest.raises(resolve.StatutoryValueMissingError):
            resolve.parameter_value(code, ON, sector=sector, sector_area=made["AREA_A"])


def test_the_void_maternity_cap_is_nowhere_in_the_loaded_data(maternity):
    """cl 13.3 caps the return at twelve weeks after the birth where the read-in
    BCEA s25(1) gives roughly seventeen and a half, and s49(1)(d) forbids
    reducing s25. The rule set keeps the Act's own figures, unchanged, and no
    scoped override exists to move them."""
    from statutory.models import StatutoryParameter

    sector, made = maternity
    rules = resolve.leave_rules(sector, ON, made["AREA_B"])

    assert rules.parental_leave_total_months == 4
    assert rules.parental_leave_additional_days == 10
    assert rules.maternity_no_work_weeks_after_birth == 6
    assert rules.maternity_earliest_start_weeks_before_birth == 4

    assert not StatutoryParameter.objects.filter(
        parameter_code__startswith="MATERNITY_MAX"
    ).exists()


# ------ P2 chunk C: the verification pass, and what it found in the load


def test_no_area_b_row_is_in_force_before_the_agreement_binds_anyone(loaded):
    """THE DEFECT THE VERIFICATION PASS FOUND (D-247).

    Clause 2(1): "This Agreement shall only come into operation from the 1st day
    of the month following the date of promulgation by the Minister", and GN
    R.7296 binds non-parties "with effect from the first day of the month after
    the date of publication of this Notice". Published 27 March 2026, so
    1 April 2026 — which is the version's own ``applies_from`` and the
    ``effective_from`` on every wage rate.

    The rule sets and notice bands went in at 2026-03-01 instead, because they
    were built through helpers carrying the 1 March constant every OTHER
    instrument in this repo uses. A month of KwaZulu-Natal employees would have
    been given this agreement's leave, hours, bonus and notice terms while the
    PREDECESSOR agreement still governed them (O-30) — including the contested
    notice refusal, which would have blocked a March termination that the
    previous agreement may well have answered.

    The wage rate refusal (D-238) masks part of it and not the whole: nothing
    makes a leave accrual or a notice band wait for a wage lookup.
    """
    from statutory.models import (
        LeaveRuleSet,
        TerminationNoticeBand,
        TerminationRuleSet,
        WorkingTimeRuleSet,
    )

    binds_from = datetime.date(2026, 4, 1)
    offenders = []

    for model in (LeaveRuleSet, WorkingTimeRuleSet, TerminationRuleSet):
        for row in model.objects.filter(sector_area__code="AREA_B"):
            if row.effective_from < binds_from:
                offenders.append(f"{model._meta.db_table}={row.effective_from}")

    for band in TerminationNoticeBand.objects.filter(
        termination_rule_set__sector_area__code="AREA_B"
    ):
        if band.termination_rule_set.effective_from < binds_from:
            offenders.append(f"notice band {band.sequence}")

    assert not offenders, (
        f"in force before the agreement binds anyone: {offenders}. Clause 2(1) and "
        f"GN R.7296 both put that at 1 April 2026."
    )


def test_march_2026_resolves_to_sd1_for_area_b_not_to_the_bccci(loaded):
    """The same defect stated as the behaviour that matters. Before 1 April 2026
    an Area B employer is governed by whatever governed them before — which for
    the rule sets this build holds is SD1's sector-wide row, and the wage is
    refused by name (D-238) because the predecessor agreement is not loaded."""
    sector, made = loaded
    march = datetime.date(2026, 3, 15)

    rules = resolve.termination_rules(sector, march, made["AREA_B"])

    assert rules.sector_area_id is None, (
        "March 2026 must fall through to SD1's sector-wide row; the BCCCI does not "
        "bind anyone until 1 April 2026"
    )
    assert rules.annual_bonus_weeks == Decimal("4.333"), "SD1's figure, not the BCCCI's"


def test_from_the_first_of_april_area_b_is_on_the_agreement(loaded):
    """The other side of the boundary, to the day."""
    sector, made = loaded

    assert resolve.termination_rules(
        sector, datetime.date(2026, 4, 1), made["AREA_B"]
    ).annual_bonus_weeks == Decimal("4.330")
