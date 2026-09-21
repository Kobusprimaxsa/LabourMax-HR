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


def test_the_four_weeks_to_six_months_window_answers_by_probation(loaded):
    """cl 21.1(b), read again (D-277, correcting D-241). The two limbs are not
    two answers to one question: the two-week limb is general and the one-week
    limb is addressed "to an employee whilst on probation, as defined", so they
    govern different employees. The full set of boundary cases is in
    statutory/tests/test_probation_notice.py."""
    sector, made = loaded
    on, started = datetime.date(2026, 7, 1), datetime.date(2026, 4, 1)

    on_probation = resolve.notice_band(
        sector, on, employment_start_date=started, sector_area=made["AREA_B"], on_probation=True
    )
    off_probation = resolve.notice_band(
        sector, on, employment_start_date=started, sector_area=made["AREA_B"], on_probation=False
    )

    assert on_probation.notice_value == Decimal("1.00")
    assert off_probation.notice_value == Decimal("2.00")


def test_the_window_refuses_when_nobody_says_which_employee_this_is(loaded):
    """Notice is SYMMETRIC (D-158), so there is still no safe direction to
    guess in — what changed is that the question has an answer once somebody
    supplies the one fact that decides it."""
    sector, made = loaded

    with pytest.raises(resolve.StatutoryValueMissingError) as raised:
        resolve.notice_band(
            sector,
            datetime.date(2026, 7, 1),
            employment_start_date=datetime.date(2026, 4, 1),
            sector_area=made["AREA_B"],
        )

    message = str(raised.value)
    assert "still on probation" in message
    assert "did not say" in message


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


# ------- D-260: the predecessor's rules, and the clause numbering that differs


@pytest.fixture
def both_agreements(loaded):
    """The predecessor's rule sets loaded under the 2026 ones."""
    for name in ("ref-2023.04.01-bccci-rules.json",):
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))
    return loaded


MARCH_2026 = datetime.date(2026, 3, 15)


def test_march_2026_resolves_area_bs_own_leave_and_working_time_rules(both_agreements):
    """Before this, March 2026 fell through to SD1's sector-wide rows — the
    wage was the agreement's and the conditions were not."""
    sector, made = both_agreements

    leave = resolve.leave_rules(sector, MARCH_2026, made["AREA_B"])
    working = resolve.working_time_rules(sector, MARCH_2026, made["AREA_B"])

    assert leave.sector_area_id == made["AREA_B"].pk
    assert working.sector_area_id == made["AREA_B"].pk
    assert "Notice 1726 of 2023" in leave.source_reference
    assert "Notice 1726 of 2023" in working.source_reference


def test_the_predecessors_figures_are_the_successors_figures(both_agreements):
    """Read off the 2023 gazette clause by clause and then compared, not
    assumed. Every figure in both rule sets matches — which is a finding about
    two instruments, not a shortcut taken while loading one."""
    sector, made = both_agreements
    before = resolve.working_time_rules(sector, MARCH_2026, made["AREA_B"])
    after = resolve.working_time_rules(sector, datetime.date(2026, 4, 1), made["AREA_B"])

    for field in (
        "ordinary_hours_per_week",
        "ordinary_hours_per_day_5day",
        "ordinary_hours_per_day_6day",
        "overtime_multiplier",
        "max_overtime_hours_per_day",
        "max_overtime_hours_per_week",
        "sunday_multiplier_ordinary",
        "sunday_multiplier_non_ordinary",
        "public_holiday_worked_multiplier",
        "min_paid_hours_per_day",
        "night_allowance_value",
        "daily_rest_hours",
        "weekly_rest_hours",
    ):
        assert getattr(before, field) == getattr(after, field), field

    assert before.pk != after.pk, "two rows, two instruments, two citations"


def test_the_long_service_band_applied_in_march_2026_too(both_agreements):
    """Clause 8.1(b) of the 2023 agreement, which is clause 9.1(b) of the 2026
    one. Twelve years' service is 28 consecutive days either side of 1 April."""
    sector, made = both_agreements
    started = datetime.date(2014, 1, 1)

    assert resolve.annual_leave_days(
        sector,
        MARCH_2026,
        employment_start_date=started,
        six_day_week=False,
        sector_area=made["AREA_B"],
    ) == Decimal("20.000")


def test_the_two_rule_sets_abut_at_the_first_of_april(both_agreements):
    """The predecessor closes exactly where the successor opens. An overlap
    would have been refused by the exclusion constraint; only a test catches a
    gap."""
    sector, made = both_agreements

    for day, scoped in (
        (datetime.date(2026, 3, 31), "Notice 1726 of 2023"),
        (datetime.date(2026, 4, 1), "GN R.7296"),
    ):
        assert scoped in resolve.leave_rules(sector, day, made["AREA_B"]).source_reference
        assert scoped in resolve.working_time_rules(sector, day, made["AREA_B"]).source_reference


def test_the_predecessors_citations_use_its_own_clause_numbers(both_agreements):
    """THE TRAP THIS AVOIDS. The 2026 agreement inserted "5. MINIMUM HOURS" and
    pushed every later clause down by one, so its working time clause is 8 and
    the predecessor's is 7. Citing the predecessor by the successor's numbers
    would point a verifier at Payment of Remuneration."""
    sector, made = both_agreements

    working = resolve.working_time_rules(sector, MARCH_2026, made["AREA_B"])
    leave = resolve.leave_rules(sector, MARCH_2026, made["AREA_B"])

    assert "clauses 3, 4.3, 4.6, 7, 10 and 15" in working.source_reference
    assert "clauses 8, 9 and 11" in leave.source_reference
    assert "clause 8" not in working.source_reference.split(", clauses ")[1]


def test_march_2026_termination_still_falls_back_and_that_is_recorded(both_agreements):
    """The honest remaining gap (D-260). The 2023 gazette's termination and
    notice provisions were not read, so the December bonus quantity for March
    2026 is still SD1's 4,333 rather than this council's 4,33."""
    sector, made = both_agreements

    rules = resolve.termination_rules(sector, MARCH_2026, made["AREA_B"])

    assert rules.sector_area_id is None, "SD1's sector-wide row, not the agreement's"
    assert rules.annual_bonus_weeks == Decimal("4.333")


# --------- D-261: termination and notice, and a defect older than the successor


@pytest.fixture
def with_termination(both_agreements):
    load_reference_data(
        json.loads(
            (REFERENCE / "ref-2023.04.01-bccci-termination.json").read_text(encoding="utf-8")
        )
    )
    return both_agreements


def test_march_2026_prices_the_bonus_and_severance_from_the_agreement(with_termination):
    """Before this, Area B's March 2026 December bonus was SD1's 4,333 — a
    figure from an instrument that did not bind these employees, and one that
    looks almost identical to the one that did."""
    sector, made = with_termination

    rules = resolve.termination_rules(sector, MARCH_2026, made["AREA_B"])

    assert rules.sector_area_id == made["AREA_B"].pk
    assert rules.annual_bonus_weeks == Decimal("4.330")
    assert rules.severance_weeks_per_completed_year == Decimal("1.00")
    assert rules.severance_requires_operational_reason is True
    assert "Notice 1726 of 2023" in rules.source_reference
    assert "clauses 4.5, 28, 29 and 35" in rules.source_reference


def test_the_probation_wording_is_older_than_the_2026_agreement(with_termination):
    """THE FINDING (D-261). Clause 20.1(b) here carries the identical wording
    the successor carries at 21.1(b) — two items both printed "i)", and a
    probation limb over the second. It was not introduced in 2026; it has been
    in the gazette since at least March 2023, and D-277 reads both the same
    way."""
    sector, made = with_termination

    band = resolve.notice_band(
        sector,
        MARCH_2026,
        employment_start_date=datetime.date(2025, 12, 15),
        sector_area=made["AREA_B"],
        on_probation=True,
    )

    assert band.notice_value == Decimal("1.00")
    assert band.notice_unit == "weeks"
    assert "clause 20.1(b)" in band.source_reference, (
        "the predecessor's own clause number, not the successor's"
    )


def test_march_2026_reads_its_own_agreement_and_not_sd1(with_termination):
    """Stated plainly because it is a CHANGE in what the system answers, and
    the right one. Area B used to fall through to SD1 and get four weeks: a
    confident answer from an instrument that did not bind these employees. The
    one that did gives one week on probation and two weeks off it — different
    figures, from the right document."""
    sector, made = with_termination
    started = datetime.date(2025, 12, 15)

    assert resolve.notice_band(
        sector, MARCH_2026, employment_start_date=started, sector_area=made["AREA_A"]
    ).notice_value == Decimal("4.00"), "SD1 still answers four weeks for Area A"

    area_b = resolve.notice_band(
        sector,
        MARCH_2026,
        employment_start_date=started,
        sector_area=made["AREA_B"],
        on_probation=False,
    )
    assert area_b.notice_value == Decimal("2.00")


def test_the_unambiguous_bands_still_answer_in_march_2026(with_termination):
    """Only the middle band refuses. One working day in the first four weeks and
    two weeks after six months are both unambiguous in this agreement too."""
    sector, made = with_termination

    first = resolve.notice_band(
        sector,
        MARCH_2026,
        employment_start_date=datetime.date(2026, 3, 1),
        sector_area=made["AREA_B"],
    )
    later = resolve.notice_band(
        sector,
        MARCH_2026,
        employment_start_date=datetime.date(2025, 6, 1),
        sector_area=made["AREA_B"],
    )

    assert (first.notice_value, first.notice_unit) == (Decimal("1.00"), "days")
    assert (later.notice_value, later.notice_unit) == (Decimal("2.00"), "weeks")


def test_the_two_agreements_notice_bands_do_not_collide(with_termination):
    """Each agreement's bands hang off its own termination rule set, so the two
    sets coexist and the right set answers on each side of 1 April."""
    sector, made = with_termination

    before = resolve.termination_rules(sector, datetime.date(2026, 3, 31), made["AREA_B"])
    after = resolve.termination_rules(sector, datetime.date(2026, 4, 1), made["AREA_B"])

    assert before.pk != after.pk
    assert before.notice_bands.count() == 4, "three service ranges, the middle one in two lanes"
    assert after.notice_bands.count() == 4
    assert "Notice 1726 of 2023" in before.notice_bands.first().source_reference
    assert "GN R.7296" in after.notice_bands.first().source_reference
