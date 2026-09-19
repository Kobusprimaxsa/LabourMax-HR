"""Builds the sector rule set fixture — leave, working time and termination.

Two rows per rule set: the BCEA default, which applies wherever a sectoral
determination is silent, and the domestic sector's overrides from Sectoral
Determination 7. **Contract cleaning is not here.** Sectoral Determination 1 was not
read for this load, so loading a contract cleaning rule set would mean inheriting the
BCEA defaults under a citation that says SD1 — which is worse than having no row,
because the fallback to the BCEA row is at least visible.

Where a rule set column has no statutory figure behind it, it is loaded as zero with
a note saying so, never as a plausible-looking number. There are four:
the three standby columns (the concept is SD7's; the Act has no standby regime).
Two are NOT among them any more, and both became explicit pairs for the same reason.
The accommodation cap is ``accommodation_deduction_capped`` plus a nullable percentage
(D-198 amended). The night allowance is ``night_allowance_type`` - now a checked
enum - plus a nullable value, NULL for ``by_agreement`` and ``time_off`` (O-22): BCEA
s17(2)(a) requires an allowance and states no amount, and a 0.0000 standing in for
that would have read as "pay nothing" the moment P7 priced night work.

Three figures here are derivations rather than transcriptions, and each carries a
note on its row saying so: 15 and 18 working days from the Act's "21 consecutive
days", the monthly accrual from that, and 3 overtime hours a day from the Act's
12-hour total-day cap less a nine-hour ordinary day.

Run: python tools/build_rule_set_fixture.py > reference/ref-2026.03.01-rules.json
"""

from __future__ import annotations

import json

BCEA = "Basic Conditions of Employment Act 75 of 1997"
BCEA_SUMMARY_URL = "https://lrs.org.za/wp-content/uploads/2024/05/Summary-of-the-BCEA.pdf"

SD7 = (
    "Sectoral Determination 7: Domestic Worker Sector, published in Regulation "
    "Gazette No. 7434 (consolidated text)"
)
SD7_URL = "https://static.pmg.org.za/docs/100824gazette_0.pdf"

ZACC = (
    "Van Wyk and Others v Minister of Employment and Labour [2025] ZACC 20, "
    "3 October 2025 (interim reading-in, suspended 36 months)"
)

EFFECTIVE_FROM = "2026-03-01"

EFFECTIVE_NOTE = (
    "effective_from is the start of this reference data, not the provision's "
    "commencement. The rule itself is older."
)

DERIVED_LEAVE_DAYS = (
    "DERIVED, not transcribed. The Act grants 21 consecutive days, which is 15 "
    "working days on a five-day week and 18 on a six-day week. The monthly accrual "
    "follows from that over a 12-month cycle. The Act states none of these four "
    "figures directly."
)

DERIVED_DAILY_OVERTIME = (
    "DERIVED, not transcribed. BCEA s10(1)(b) caps the whole day at 12 hours "
    "including overtime; three is that cap less a nine-hour ordinary day. On an "
    "eight-hour six-day week the same cap allows four."
)

NO_STATUTORY_FIGURE = (
    "ZERO MEANS THERE IS NO STATUTORY FIGURE, not that the amount is nil. "
    "BCEA s17(2) requires night work to be compensated by an allowance or by reduced "
    "hours, and deliberately sets no amount - the employer sets it. A calculator must "
    "read the employer's own setting, not this column."
)

NO_BCEA_STANDBY = (
    "ZERO MEANS THE CONCEPT DOES NOT EXIST IN THE ACT. Standby is a Sectoral "
    "Determination 7 creature; the BCEA has no standby regime at all."
)


def no_accommodation_cap(instrument):
    """The citation for the BOOLEAN. "This instrument states no accommodation
    percentage" is a statutory reading and carries its source exactly as a figure
    does - and it is the ABSENCE of a cap, not a cap of zero, which is a different
    reading the old 0.00 sentinel could not tell apart (D-198 amended)."""
    return (
        f"accommodation_deduction_capped = false: {instrument} states no accommodation "
        "percentage, so no cap is loaded and accommodation_deduction_max_pct is null. "
        "That is the ABSENCE of a cap, not a cap of zero. BCEA s34 governs deductions "
        "generally but states no percentage; the 10 percent cap is SD7's."
    )


NO_STATUTORY_BONUS = (
    "There is no statutory annual bonus in this sector, so the weeks are zero and the "
    "month is NULL. Contract cleaning is the sector that has one."
)

HAS_STATUTORY_BONUS = (
    "SD1 clause 3(3): 4,333 weeks of the weekly wage, paid in December or on "
    "termination. Under twelve months' service it is pro-rated as full calendar "
    "months divided by twelve, times 4,333, times the weekly wage - so a single full "
    "month earns a share, which is why the minimum service is zero."
)


def leave_rules(*, sector, source, family_days, extra_notes=""):
    notes = [EFFECTIVE_NOTE, DERIVED_LEAVE_DAYS]
    if extra_notes:
        notes.append(extra_notes)
    row = {
        "effective_from": EFFECTIVE_FROM,
        "source_reference": source,
        "notes": " ".join(notes),
        "annual_leave_days_per_cycle_5day": "15.000",
        "annual_leave_days_per_cycle_6day": "18.000",
        "annual_accrual_days_per_month_5day": "1.250",
        "annual_accrual_days_per_month_6day": "1.500",
        "annual_accrual_ratio_days_worked": 17,
        "annual_accrual_ratio_hours_worked": 17,
        "annual_leave_cycle_months": 12,
        "annual_leave_forfeit_months": 6,
        "annual_leave_payable_on_termination": True,
        "sick_leave_cycle_months": 36,
        "sick_leave_weeks_equivalent": "6.00",
        "sick_leave_first_six_months_ratio": 26,
        "sick_leave_payable_on_termination": False,
        "family_responsibility_days": family_days,
        "family_resp_min_service_months": 4,
        "family_resp_min_days_per_week": 4,
        "parental_leave_total_months": 4,
        "parental_leave_additional_days": 10,
        "parental_leave_shareable": True,
        "maternity_earliest_start_weeks_before_birth": 4,
        "maternity_no_work_weeks_after_birth": 6,
    }
    if sector:
        row["sector"] = sector
    return row


def working_time_rules(
    *,
    sector,
    source,
    max_overtime_week,
    standby_allowance,
    standby_start,
    standby_end,
    standby_hours,
    accommodation_capped,
    accommodation_pct=None,
    accommodation_source=None,
    night_allowance_type="by_agreement",
    night_allowance_value=None,
    min_paid_hours="4.00",
    extra_notes="",
):
    notes = [EFFECTIVE_NOTE, DERIVED_DAILY_OVERTIME]
    if night_allowance_type == "by_agreement":
        notes.append(NO_STATUTORY_FIGURE)
    if not standby_allowance or standby_allowance == "0.00":
        notes.append(NO_BCEA_STANDBY)
    if not accommodation_capped:
        notes.append(no_accommodation_cap(accommodation_source or source))
    if extra_notes:
        notes.append(extra_notes)

    row = {
        "effective_from": EFFECTIVE_FROM,
        "source_reference": source,
        "notes": " ".join(notes),
        "ordinary_hours_per_week": "45.00",
        "ordinary_hours_per_day_5day": "9.00",
        "ordinary_hours_per_day_6day": "8.00",
        "overtime_multiplier": "1.500",
        "max_overtime_hours_per_day": "3.00",
        "max_overtime_hours_per_week": max_overtime_week,
        "sunday_multiplier_non_ordinary": "2.000",
        "sunday_multiplier_ordinary": "1.500",
        "public_holiday_worked_multiplier": "2.000",
        "public_holiday_not_worked_paid": True,
        "night_work_start_time": "18:00:00",
        "night_work_end_time": "06:00:00",
        "night_allowance_type": night_allowance_type,
        "night_allowance_value": night_allowance_value,
        "standby_allowance_per_shift": standby_allowance,
        "standby_window_start": standby_start,
        "standby_window_end": standby_end,
        "standby_hours_before_overtime": standby_hours,
        "min_paid_hours_per_day": min_paid_hours,
        "meal_interval_after_hours": "5.00",
        "meal_interval_minutes": 60,
        "daily_rest_hours": 12,
        "weekly_rest_hours": 36,
        "accommodation_deduction_capped": accommodation_capped,
        "accommodation_deduction_max_pct": accommodation_pct,
    }
    if sector:
        row["sector"] = sector
    return row


def termination_rules(
    *,
    sector,
    source,
    bonus_weeks="0.000",
    bonus_month=None,
    bonus_pro_rata=False,
    extra_notes="",
):
    """Severance and pro-rata bonus only. Notice moved to
    ``termination_notice_band`` (D-68) — see ``build_notice_band_fixture.py``.
    """
    notes = [EFFECTIVE_NOTE]
    notes.append(NO_STATUTORY_BONUS if bonus_month is None else HAS_STATUTORY_BONUS)
    if extra_notes:
        notes.append(extra_notes)
    row = {
        "effective_from": EFFECTIVE_FROM,
        "source_reference": source,
        "notes": " ".join(notes),
        "severance_weeks_per_completed_year": "1.00",
        "severance_requires_operational_reason": True,
        "annual_bonus_weeks": bonus_weeks,
        "annual_bonus_month": bonus_month,
        "annual_bonus_pro_rata_on_termination": bonus_pro_rata,
        "annual_bonus_min_service_months": 0,
    }
    if sector:
        row["sector"] = sector
    return row


DOCUMENT = {
    "version_label": "REF-2026.03.01-RULES-r3",
    "applies_from": "2026-03-01",
    "description": (
        "Sector rule sets: the BCEA default and the domestic sector's Sectoral "
        "Determination 7 overrides, for leave, working time and termination. Contract "
        "cleaning is deliberately absent - SD1 has not been read. Researched "
        "13 September 2026. NOT verified."
    ),
    "tables": {
        "leave_rule_set": [
            leave_rules(
                sector=None,
                source=f"{BCEA}, ss 20-27",
                family_days=3,
                extra_notes=(
                    f"Parental leave is not the Act's four months: {ZACC} All parents "
                    f"together have four months and ten days, divided as they agree. "
                    f"Family responsibility leave is three days under s27(2); the "
                    f"qualifying conditions of four months' service and four days a week "
                    f"are s27(1)."
                ),
            ),
            leave_rules(
                sector="DOMESTIC",
                source=f"{SD7}, read with {BCEA} ss 20-27",
                family_days=5,
                extra_notes=(
                    f"SD7 gives FIVE days' family responsibility leave, not the Act's "
                    f"three. Annual and sick leave match the Act. Parental leave is the "
                    f"same interim position: {ZACC}"
                ),
            ),
        ],
        "working_time_rule_set": [
            working_time_rules(
                sector=None,
                source=f"{BCEA}, ss 9-18 and s9A",
                max_overtime_week="10.00",
                standby_allowance="0.00",
                standby_start="00:00:00",
                standby_end="00:00:00",
                standby_hours="0.00",
                accommodation_capped=False,
                accommodation_source="the BCEA",
                extra_notes=(
                    "Overtime is capped at 10 hours a week under s10(1)(b); a collective "
                    "agreement may take it to 15 for up to two months a year, which is an "
                    "agreement-level fact rather than a statutory default. The four-hour "
                    "minimum payment is s9A and applies to employees earning below the "
                    "s6(3) threshold."
                ),
            ),
            working_time_rules(
                sector="DOMESTIC",
                source=SD7,
                max_overtime_week="15.00",
                standby_allowance="20.00",
                standby_start="20:00:00",
                standby_end="06:00:00",
                standby_hours="3.00",
                accommodation_capped=True,
                accommodation_pct="10.00",
                extra_notes=(
                    "VERIFY THE STANDBY ALLOWANCE. R20 per shift is the figure in the "
                    "determination as published and no later amendment was found. It may "
                    "well have escalated - it is the one figure in this load most likely "
                    "to be stale. SD7 also caps standby at five shifts a month and fifty "
                    "a year, which this table has no column for. Overtime in this sector "
                    "is 15 hours a week, not the Act's 10. The 20:00-06:00 standby window "
                    "comes from a secondary summary rather than the gazette text."
                ),
            ),
        ],
        "termination_rule_set": [
            termination_rules(
                sector=None,
                source=f"{BCEA}, ss 37 and 41",
            ),
            termination_rules(
                sector="DOMESTIC",
                source=f"{SD7}, read with {BCEA} s37(1)(c)",
            ),
        ],
    },
}


SD1 = (
    "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text "
    "(clauses 3, 8-24)"
)
SD1_URL = "https://www.acts.co.za/basic/sd1_nr622_3__remuneration.php"

DOCUMENT_SD1 = {
    "version_label": "REF-2026.03.01-SD1-r3",
    "applies_from": "2026-03-01",
    "description": (
        "Contract cleaning sector rule sets from Sectoral Determination 1, clauses 3 "
        "and 8 to 24. Researched 13 September 2026 from two independent transcriptions "
        "of the consolidated determination. NOT verified."
    ),
    "tables": {
        "leave_rule_set": [
            leave_rules(
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clauses 18, 19 and 22",
                family_days=3,
                extra_notes=(
                    f"Annual leave, sick leave and family responsibility leave all match "
                    f"the BCEA - SD1 adds nothing here, unlike SD7 which gives five "
                    f"family responsibility days. Parental leave is the same interim "
                    f"position: {ZACC}"
                ),
            )
        ],
        "working_time_rule_set": [
            working_time_rules(
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clauses 3(2), 8 to 17",
                max_overtime_week="10.00",
                standby_allowance="0.00",
                standby_start="00:00:00",
                standby_end="00:00:00",
                standby_hours="0.00",
                accommodation_capped=False,
                accommodation_source="Sectoral Determination 1",
                night_allowance_type="percentage",
                night_allowance_value="10.0000",
                min_paid_hours="6.00",
                extra_notes=(
                    "Two figures here are SD1's own and differ from every other row. "
                    "Clause 16 sets a REAL night allowance - not less than 10 percent of "
                    "the hourly wage for each hour worked between 18:00 and 06:00 - so "
                    "this is the one sector where the column carries a gazetted "
                    "percentage rather than a zero meaning 'no statutory figure'. And "
                    "clause 3(2) sets the short-day minimum at SIX hours, not the four "
                    "of BCEA s9A and SD7. "
                    "Overtime: clause 9 caps it at 3 hours a day and 10 a week for an "
                    "employer with ten or more employees, and allows 15 a week by "
                    "agreement for a smaller employer. The stricter general rule is "
                    "loaded; the small-employer variant is an agreement-level fact this "
                    "table has no column for. "
                    "Standby and accommodation are SD7 concepts; SD1 has neither."
                ),
            )
        ],
        "termination_rule_set": [
            termination_rules(
                sector="CONTRACT_CLEANING",
                source=f"{SD1}, clauses 3(3) and 24",
                bonus_weeks="4.333",
                bonus_month=12,
                bonus_pro_rata=True,
            )
        ],
    },
}


if __name__ == "__main__":
    import sys

    which = sys.argv[1] if len(sys.argv) > 1 else "bcea"
    print(json.dumps(DOCUMENT_SD1 if which == "sd1" else DOCUMENT, indent=2, ensure_ascii=False))
