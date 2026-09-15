"""The fixture that ships with the repo, loaded and checked on every commit.

Everything else in this app tests the machinery. This tests the data: the actual
1 March 2026 figures, loaded through the actual loader, checked by the actual
arithmetic. If someone edits a digit in ``reference/ref-2026.03.01.json``, this is
what notices.

It is deliberately not a golden test. A golden test reproduces a published worked
example and proves the figures are *right*; this proves the file loads and hangs
together. The golden tests arrive with the calculators in P7, and they are what
finally lets a reference version be marked ``golden_tests_passed``.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal

import pytest

from statutory import checks, resolve
from statutory.loader import load_reference_data
from statutory.models import MinimumWageRate, PublicHoliday, Sector

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "reference" / "ref-2026.03.01.json"

MARCH_2026 = datetime.date(2026, 3, 1)


@pytest.fixture
def loaded(db):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


@pytest.mark.statutory
def test_the_shipped_fixture_exists():
    assert FIXTURE.exists(), f"{FIXTURE} is missing - the reference data ships with the repo."


@pytest.mark.statutory
def test_the_shipped_fixture_loads(loaded):
    assert loaded.total_created > 0
    assert not loaded.closed_periods, "A first load has nothing to supersede."


@pytest.mark.statutory
def test_the_shipped_fixture_reconciles(loaded):
    blocking = [issue for issue in checks.run_all() if issue.blocking]
    assert not blocking, "\n".join(str(issue) for issue in blocking)


@pytest.mark.statutory
def test_every_loaded_rate_carries_a_real_citation(loaded):
    for rate in MinimumWageRate.objects.all():
        assert len(rate.source_reference) > 20, (
            f"{rate} is cited as {rate.source_reference!r}, which is too short to find "
            f"the source again."
        )


@pytest.mark.statutory
def test_the_national_minimum_wage_resolves_on_the_day_it_takes_effect(loaded):
    rate = resolve.minimum_wage(MARCH_2026)
    assert rate.sector is None
    assert rate.hourly_rate == Decimal("30.2300")


@pytest.mark.statutory
def test_a_domestic_employer_resolves_to_the_domestic_row_not_the_fallback(loaded):
    domestic = Sector.objects.get(code=Sector.Code.DOMESTIC)
    rate = resolve.minimum_wage(MARCH_2026, sector=domestic)
    assert rate.sector == domestic


@pytest.mark.statutory
def test_the_day_before_the_gazette_takes_effect_resolves_to_nothing(loaded):
    """There is no earlier row loaded, and the system says so rather than guessing."""
    with pytest.raises(resolve.StatutoryValueMissingError):
        resolve.minimum_wage(MARCH_2026 - datetime.timedelta(days=1))


@pytest.mark.statutory
def test_kwazulu_natal_contract_cleaning_has_no_rate_and_falls_back_visibly(loaded):
    """The gazette gives Area B no figure, so none was invented.

    A KwaZulu-Natal employer therefore resolves to the sector-wide fallback, which is
    wrong for them — the BCCCI agreement governs. That is why the area is flagged
    ``uses_bargaining_council_rates`` and why the watch list carries it as not loaded:
    onboarding must refuse a KwaZulu-Natal contract cleaning employer until the
    agreement is in hand.

    KwaZulu-Natal is **Area B**, not Area C. D-61 had it the other way round and D-118
    corrected it; this test asserts the corrected lettering.
    """
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    kzn = cleaning.areas.get(code="AREA_B")

    assert kzn.uses_bargaining_council_rates is True
    assert not MinimumWageRate.objects.filter(sector_area=kzn).exists()


@pytest.mark.statutory
def test_the_contract_cleaning_area_lettering_is_the_gazettes_and_not_d61s(loaded):
    """D-61 read the gazette's three columns wrongly and D-118 corrected it.

    Getting this wrong is not cosmetic. Reading the listed Local Councils as a separate
    Area B at the low rate underpaid every one of them by R2,94 an hour, and putting
    KwaZulu-Natal in Area C left the residual — most of the country — with no rate at
    all. The correction lives in data, so only a test holds it in place.
    """
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    assert set(cleaning.areas.values_list("code", flat=True)) == {"AREA_A", "AREA_B", "AREA_C"}

    area_a = cleaning.areas.get(code="AREA_A")
    area_c = cleaning.areas.get(code="AREA_C")

    # The metros AND the listed local councils share one column, at the HIGH rate.
    assert area_a.municipalities.count() == 12
    assert {m.municipality_type for m in area_a.municipalities.all()} == {"metro", "local"}

    high = MinimumWageRate.objects.get(sector_area=area_a).hourly_rate
    low = MinimumWageRate.objects.get(sector_area=area_c).hourly_rate
    assert high > low, "Area A is the high-rate column"

    # Area B is a province and Area C a residual, so neither is a list.
    assert cleaning.areas.get(code="AREA_B").municipalities.count() == 0
    assert area_c.municipalities.count() == 0


def test_the_builder_still_reproduces_the_shipped_fixture():
    """``tools/build_reference_fixture.py`` is the source; the JSON is its output.

    They drifted apart once: the fixture was hand-corrected for D-118 and the builder
    was not, so the next regeneration would have silently reinstated D-61's wrong
    lettering and the R2,94 underpayment with it. Nothing else would have noticed.
    """
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    tools = str(root / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from build_reference_fixture import DOCUMENT

    shipped = json.loads((root / "reference" / "ref-2026.03.01.json").read_text(encoding="utf-8"))
    assert DOCUMENT == shipped, "regenerate reference/ref-2026.03.01.json from the builder"


@pytest.mark.statutory
def test_the_bcea_threshold_changes_on_the_first_of_may(loaded):
    """Two rows, because the threshold moved inside this reference version's life."""
    april = resolve.parameter_value("BCEA_EARNINGS_THRESHOLD", datetime.date(2026, 4, 30))
    may = resolve.parameter_value("BCEA_EARNINGS_THRESHOLD", datetime.date(2026, 5, 1))

    assert april == Decimal("261748.450000")
    assert may == Decimal("269600.900000")


@pytest.mark.statutory
def test_the_uif_ceiling_and_rates_resolve(loaded):
    assert resolve.parameter_value("UIF_MONTHLY_CEILING", MARCH_2026) == Decimal("17712.000000")
    assert resolve.parameter_value("UIF_EMPLOYEE_RATE_PCT", MARCH_2026) == Decimal("1.000000")
    assert resolve.parameter_value("UIF_EMPLOYER_RATE_PCT", MARCH_2026) == Decimal("1.000000")


@pytest.mark.statutory
def test_the_2026_womens_day_shift_is_stored_as_the_monday(loaded):
    """9 August 2026 is a Sunday, so the holiday is the 10th — stored, not computed."""
    monday = PublicHoliday.objects.get(holiday_date=datetime.date(2026, 8, 10))
    assert monday.shifted_from_date == datetime.date(2026, 8, 9)
    assert resolve.is_public_holiday(datetime.date(2026, 8, 9)) is False
    assert resolve.is_public_holiday(datetime.date(2026, 8, 10)) is True


@pytest.mark.statutory
def test_both_calendar_years_are_loaded_in_full(loaded):
    for year in (2026, 2027):
        count = PublicHoliday.objects.filter(
            holiday_date__year=year,
        ).count()
        assert count == 12, f"{year} has {count} public holidays loaded, expected 12."


@pytest.mark.statutory
def test_the_tax_year_covers_its_own_dates(loaded):
    year = resolve.tax_year(MARCH_2026)
    assert year.label == "2026/2027"
    assert len(resolve.paye_brackets(year)) == 7
    assert len(resolve.paye_rebates(year, age=68)) == 2


@pytest.mark.statutory
def test_the_version_is_loaded_but_not_in_force(loaded):
    """Nothing can run payroll against this until a person has checked every figure."""
    from statutory.models import ReferenceDataVersion

    version = ReferenceDataVersion.objects.get(version_label="REF-2026.03.01")
    assert version.verified_at is None
    assert version.is_usable is False
    assert ReferenceDataVersion.in_force_on(MARCH_2026) is None


# ------------------------------------------------------------------- rule sets

RULES_FIXTURE = FIXTURE.parent / "ref-2026.03.01-rules.json"


@pytest.fixture
def rules_loaded(loaded):
    document = json.loads(RULES_FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


SD1_FIXTURE = FIXTURE.parent / "ref-2026.03.01-sd1.json"


@pytest.fixture
def sd1_loaded(rules_loaded):
    document = json.loads(SD1_FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


NOTICE_BANDS_FIXTURE = FIXTURE.parent / "ref-2026.03.01-notice-bands.json"


@pytest.fixture
def notice_bands_loaded(sd1_loaded):
    document = json.loads(NOTICE_BANDS_FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


@pytest.mark.statutory
def test_the_rule_set_fixture_loads(rules_loaded):
    assert rules_loaded.created["leave_rule_set"] == 2
    assert rules_loaded.created["working_time_rule_set"] == 2
    assert rules_loaded.created["termination_rule_set"] == 2


@pytest.mark.statutory
def test_the_domestic_sector_overrides_the_bcea_where_sd7_differs(notice_bands_loaded):
    """Three places SD7 departs from the Act, and getting any of them wrong
    underpays or over-restricts every domestic employee in the system."""
    domestic = Sector.objects.get(code=Sector.Code.DOMESTIC)
    on = datetime.date(2026, 6, 1)

    assert resolve.leave_rules(domestic, on).family_responsibility_days == 5
    assert resolve.leave_rules(None, on).family_responsibility_days == 3

    assert resolve.working_time_rules(domestic, on).max_overtime_hours_per_week == Decimal("15.00")
    assert resolve.working_time_rules(None, on).max_overtime_hours_per_week == Decimal("10.00")

    # One day more than six months before `on` — BCEA s37(1)(a) and SD7 both put
    # EXACTLY six months in the lower band (D-158, corrected), so this has to be
    # past it to tell the two sectors' upper bands apart.
    started = datetime.date(2025, 11, 30)
    assert resolve.notice_band(domestic, on, employment_start_date=started).notice_value == 4
    assert resolve.notice_band(None, on, employment_start_date=started).notice_value == 2


@pytest.mark.statutory
def test_contract_cleaning_carries_its_own_rules_from_sd1(sd1_loaded):
    """The three places SD1 is its own creature, and each is a real money difference."""
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    on = datetime.date(2026, 6, 1)

    working_time = resolve.working_time_rules(cleaning, on)
    assert working_time.sector == cleaning
    assert working_time.night_allowance_type == "percentage"
    assert working_time.night_allowance_value == Decimal("10.0000")
    assert working_time.min_paid_hours_per_day == Decimal("6.00")

    termination = resolve.termination_rules(cleaning, on)
    assert termination.annual_bonus_weeks == Decimal("4.333")
    assert termination.annual_bonus_month == 12
    assert termination.annual_bonus_pro_rata_on_termination is True


@pytest.mark.statutory
def test_the_short_day_minimum_differs_by_sector(sd1_loaded):
    """Six hours under SD1 clause 3(2), four under BCEA s9A and SD7. Paying four to a
    contract cleaner who worked two hours underpays them by two hours."""
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    domestic = Sector.objects.get(code=Sector.Code.DOMESTIC)
    on = datetime.date(2026, 6, 1)

    assert resolve.working_time_rules(cleaning, on).min_paid_hours_per_day == Decimal("6.00")
    assert resolve.working_time_rules(domestic, on).min_paid_hours_per_day == Decimal("4.00")


@pytest.mark.statutory
def test_the_night_allowance_is_a_real_figure_only_in_contract_cleaning(sd1_loaded):
    """SD1 clause 16 gazettes 10 percent of the hourly wage. The BCEA sets no amount
    at all, and the zero on that row means exactly that."""
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    on = datetime.date(2026, 6, 1)

    assert resolve.working_time_rules(cleaning, on).night_allowance_value == Decimal("10.0000")

    bcea = resolve.working_time_rules(None, on)
    assert bcea.night_allowance_value == Decimal("0.0000")
    assert "NO STATUTORY FIGURE" in bcea.notes


@pytest.mark.statutory
def test_sd1_notice_splits_at_four_weeks_of_service_not_six_months(notice_bands_loaded):
    """D-68, closed. SD1 clause 23(1) splits notice at FOUR WEEKS of service, and
    the shipped fixture now carries that boundary rather than working around it.
    """
    cleaning = Sector.objects.get(code=Sector.Code.CONTRACT_CLEANING)
    on = datetime.date(2026, 6, 1)

    just_under = resolve.notice_band(
        cleaning, on, employment_start_date=on - datetime.timedelta(weeks=3)
    )
    assert just_under.notice_value == 1
    assert just_under.notice_unit == "days"

    over = resolve.notice_band(cleaning, on, employment_start_date=on - datetime.timedelta(weeks=5))
    assert over.notice_value == 4
    assert over.notice_unit == "weeks"


@pytest.mark.statutory
def test_parental_leave_is_stored_as_months_plus_days(rules_loaded):
    """Four calendar months from January is not four from June. The statute says
    months, so the reference data says months."""
    rules = resolve.leave_rules(None, datetime.date(2026, 6, 1))
    assert rules.parental_leave_total_months == 4
    assert rules.parental_leave_additional_days == 10
    assert rules.parental_leave_shareable is True


@pytest.mark.statutory
def test_the_post_birth_restriction_is_stored_separately_from_the_earliest_start(rules_loaded):
    rules = resolve.leave_rules(None, datetime.date(2026, 6, 1))
    assert rules.maternity_no_work_weeks_after_birth == 6
    assert rules.maternity_earliest_start_weeks_before_birth == 4


@pytest.mark.statutory
def test_a_column_with_no_statutory_figure_says_so_in_its_notes(rules_loaded):
    """Zero in night_allowance_value means the Act sets no amount, not that the
    allowance is nil. A reader who misses that underpays every night shift."""
    rules = resolve.working_time_rules(None, datetime.date(2026, 6, 1))
    assert rules.night_allowance_value == Decimal("0.0000")
    assert "NO STATUTORY FIGURE" in rules.notes


# --------------------------------------------------------------- source codes

CODES_FIXTURE = FIXTURE.parent / "ref-2026.03.01-codes.json"


@pytest.fixture
def codes_loaded(db):
    document = json.loads(CODES_FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


@pytest.mark.statutory
def test_the_source_code_fixture_loads_and_holds_together(codes_loaded):
    from statutory.models import SarsSourceCode

    assert codes_loaded.created["sars_source_code"] > 10
    blocking = [issue for issue in checks.check_source_codes() if issue.blocking]
    assert not blocking, "\n".join(str(issue) for issue in blocking)
    assert SarsSourceCode.objects.filter(source_reference="").count() == 0


@pytest.mark.statutory
def test_commission_is_out_of_the_uif_base_and_in_every_other(codes_loaded):
    """The row the table exists for. Get this wrong and it surfaces at a UI-19
    reconciliation months later, not on the payslip."""
    from statutory.models import SarsSourceCode

    commission = SarsSourceCode.objects.get(code="3606")
    assert commission.is_taxable is True
    assert commission.is_uif_remuneration is False
    assert commission.is_sdl_remuneration is True


@pytest.mark.statutory
def test_the_bonus_code_is_in_the_uif_base_unlike_commission(codes_loaded):
    from statutory.models import SarsSourceCode

    bonus = SarsSourceCode.objects.get(code="3605")
    assert bonus.is_uif_remuneration is True


@pytest.mark.statutory
def test_every_base_flag_carries_its_reasoning(codes_loaded):
    """A boolean with no note beside it is a compliance decision nobody can audit."""
    from statutory.models import SarsSourceCode

    for row in SarsSourceCode.objects.all():
        assert len(row.notes) > 40, f"{row.code} carries no reasoning for its flags."


# ---------------------------------------------------------------------- banks

BANKS_FIXTURE = FIXTURE.parent / "ref-2026.03.01-banks.json"


@pytest.fixture
def banks_loaded(db):
    document = json.loads(BANKS_FIXTURE.read_text(encoding="utf-8"))
    return load_reference_data(document)


@pytest.mark.statutory
def test_the_bank_fixture_loads_with_valid_branch_codes(banks_loaded):
    from statutory.models import Bank

    assert banks_loaded.created["bank"] > 20
    assert not [issue for issue in checks.check_banks() if issue.blocking]

    for bank in Bank.objects.all():
        assert len(bank.universal_branch_code) == 6, f"{bank.name} has a malformed code."


@pytest.mark.statutory
def test_the_big_five_are_present_with_the_codes_employees_will_recognise(banks_loaded):
    from statutory.models import Bank

    expected = {
        "Absa Bank Limited": "632005",
        "FirstRand Bank Limited (FNB)": "250655",
        "Standard Bank of South Africa": "051001",
        "Nedbank Limited": "198765",
        "Capitec Bank Limited": "470010",
    }
    for name, code in expected.items():
        assert Bank.objects.get(name=name).universal_branch_code == code


@pytest.mark.statutory
def test_no_account_length_was_invented(banks_loaded):
    """NULL means nobody has confirmed the bank's rule. A guessed bound rejects valid
    account numbers, which ends with an employee not being paid."""
    from statutory.models import Bank

    assert not Bank.objects.exclude(account_number_min_length=None).exists()
    for bank in Bank.objects.all():
        assert "not loaded" in bank.notes
