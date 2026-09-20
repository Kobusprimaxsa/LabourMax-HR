"""Build ``reference/ref-2023.04.01-bccci.json`` — the PREDECESSOR BCCCI agreement.

The Bargaining Council for the Contract Cleaning Services Industry
(KwaZulu-Natal) Main Collective Agreement extended to non-parties by NOTICE 1726
OF 2023 in Government Gazette 48356, 31 March 2023, signed by Minister T W Nxesi.

**This is the instrument that governed 1–31 March 2026 and that O-30 was open
for.** The 2026 agreement's clause 2(1)(a) says "the parties agree that the
current Main Agreement shall continue to be enforced" and its clause 2(3)
carries prevailing terms forward until replacement; this agreement's own
extension notice binds non-parties "with effect from the first day of the month
after the date of publication of this Notice and shall remain in force until
replaced by a subsequent agreement". So its last rate ran until the 2026
agreement took effect on 1 April 2026, and before this was loaded a KwaZulu-Natal
contract cleaning payroll for March 2026 refused by name (D-238).

**Found, reported, then loaded — three passes, deliberately.** The gazette was
located and rendered on 19 September 2026, its figures transcribed and reported
on 20 September, and only then loaded. Finding and loading in one pass is what
produced the corrected citations the first time round.

**The body text is not extractable** — the PDF has no usable font encoding, so
the pages were rendered and read. Clause 4.1(a) and clause 2(1) were read off
page 39 and page 37 of the gazette respectively, and clause 3's "monthly wage"
off page 38.

**The three rates close each other and the last closes on the successor.** The
2025 rate runs to 1 April 2026 exclusive, which is the day the 2026 agreement's
own first rate begins, so the two agreements abut with neither a gap nor an
overlap for ``minimum_wage_rate``'s exclusion constraint to catch.

**What this does NOT load.** The wage rates, the monthly wage factor, the leave
and working time rule sets and the two additive maternity benefits are read and
loaded. Its TERMINATION and NOTICE provisions are not, so for 1–31 March 2026 a
severance calculation, the December bonus quantity and every notice band still
resolve to Sectoral Determination 1's sector-wide rows rather than to this
agreement. Recorded rather than papered over (D-259, D-260).

Run: python tools/build_bccci_2023_fixture.py
"""

from __future__ import annotations

import json
import pathlib

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2023.04.01-bccci.json"

#: Same shape as the 2026 agreement's citation (D-257): the council's full name,
#: the province abbreviated, and the notice and gazette that carry it. No
#: identifier is shared with the 2026 one, so checkstatutory's near-duplicate
#: check reads them as the two different documents they are.
GAZETTE = (
    "Bargaining Council for the Contract Cleaning Services Industry (KZN) "
    "Main Collective Agreement, Notice 1726 of 2023 in GG 48356, 31 March 2023"
)
GAZETTE_URL = "https://www.gov.za/sites/default/files/gcis_document/202304/48356gen1726.pdf"

DERIVED_START = (
    "DERIVED DATE, not printed. Clause 4.1(a)(i) gives this rate 'with effect from the "
    "period of operation', clause 2(1) puts the period of operation at 'the 1st day of "
    "the month following the date of promulgation', and the extension notice binds "
    "non-parties 'with effect from the first day of the month after the date of "
    "publication of this Notice'. Published 31 March 2023, therefore 1 April 2023. "
    "Check the two clauses rather than trusting this note."
)

CLOSED_BY_SUCCESSOR = (
    "CLOSED BY ITS SUCCESSOR, not by this agreement. Nothing here states an end date: "
    "the extension notice says the agreement 'shall remain in force until replaced by a "
    "subsequent agreement' and clause 2(3) carries prevailing terms forward until a new "
    "one is promulgated. So this rate governed until the 2026 agreement (GN R.7296 in "
    "GG 54412) took effect on 1 April 2026, and that is where effective_to comes from. "
    "IT IS THE RATE THAT APPLIED IN MARCH 2026, which is the month a KwaZulu-Natal "
    "payroll used to refuse on (O-30)."
)

AREA_B = (
    "Area B is all of KwaZulu-Natal. Sectoral Determination 1 states no figure for it "
    "and points at this council's agreement; sector_area.uses_bargaining_council_rates "
    "records that, and statutory/resolve.py refuses rather than falling back to the "
    "National Minimum Wage where no row is loaded."
)

NOT_A_GAZETTEER = (
    "WHAT IS STILL NOT LOADED FROM THIS GAZETTE: its termination and notice provisions. So "
    "for 1-31 March 2026 a severance calculation, the December bonus quantity and every "
    "notice band still resolve to Sectoral Determination 1's sector-wide rows rather than to "
    "this agreement (D-260)."
)

NOT_A_GAZETTEER_RULES = (
    "WHAT IS STILL NOT LOADED FROM THIS GAZETTE: its termination and notice provisions. So "
    "for 1-31 March 2026 a severance calculation, the December bonus quantity and every "
    "notice band still resolve to Sectoral Determination 1's sector-wide rows rather than to "
    "this agreement (D-260)."
)


def wage(rate: str, effective_from: str, effective_to: str, subclause: str, extra: str) -> dict:
    return {
        "sector": "CONTRACT_CLEANING",
        "sector_area": "AREA_B",
        "hours_band": "all",
        "hourly_rate": rate,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "source_reference": f"{GAZETTE}, clause 4.1(a)({subclause})",
        "source_url": GAZETTE_URL,
        "notes": " ".join(
            [
                "Clause 4.1(a): per hour or part thereof, calculated on a pro rata basis "
                "for all employees, for the province of Kwa-Zulu Natal.",
                AREA_B,
                extra,
                NOT_A_GAZETTEER,
            ]
        ),
    }


FIXTURE = {
    "version_label": "REF-2023.04.01-BCCCI-r2",
    "applies_from": "2023-04-01",
    "description": (
        "The PREDECESSOR BCCCI (KwaZulu-Natal) Main Collective Agreement: wage rates and "
        "the monthly wage factor, from Notice 1726 of 2023 in GG 48356, 31 March 2023. "
        "This is the instrument that governed 1-31 March 2026 (O-30). Read off the "
        "rendered gazette; the PDF's body text does not extract. NOT verified."
    ),
    "tables": {
        "minimum_wage_rate": [
            wage("27.5000", "2023-04-01", "2024-03-01", "i", DERIVED_START),
            wage("29.1200", "2024-03-01", "2025-03-01", "ii", ""),
            wage("30.8600", "2025-03-01", "2026-04-01", "iii", CLOSED_BY_SUCCESSOR),
        ],
        "statutory_parameter": [
            {
                "parameter_code": "MONTHLY_TO_WEEKLY_FACTOR",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "4.330000",
                "unit": "ratio",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 3, definition of 'monthly wage'",
                "source_url": GAZETTE_URL,
                "notes": (
                    "Clause 3: \"'monthly wage' shall mean the hours normally worked in a "
                    "week multiplied by the rate applicable as stipulated in clause 4 and "
                    'multiplied by 4.33." Word for word what the 2026 agreement says, so '
                    "the factor did not move when the agreement was replaced - but it is "
                    "loaded from this instrument for this period rather than reached "
                    "backwards from the next one. Without it a March 2026 monthly wage "
                    "would fall back to the unscoped BCEA s35(3) figure of 4.333333, "
                    "which is a different number and not the one that bound these "
                    "employees (D-236). Closed at 1 April 2026, where the successor's own "
                    "scoped row begins."
                ),
            }
        ],
    },
}


# ---------------------------------------------------------------------------
# THE RULE SETS, read clause by clause off the same gazette.
#
# THE CLAUSE NUMBERING IS NOT THE 2026 AGREEMENT'S AND MUST NOT BE MAPPED ONTO
# IT (D-260). The 2026 agreement inserted "5. MINIMUM HOURS" as its own clause
# and pushed everything after it down by one, so where 2026 has clause 8
# Regulation of Working Time, 9 Annual Leave, 10 Sick Leave, 11 Public Holidays
# and Sundays, 12 Study Leave and 13 Maternity, THIS agreement has 7, 8, 9, 10,
# 11 and 12 - and its six-hour minimum is clause 4.6(a) rather than a clause of
# its own. Every citation below was read off the page, not derived by
# subtracting one.
#
# That renumbering also explains a gazette error already recorded in O-32: the
# 2026 agreement's clause 3 defines "overtime" by reference to "Clause 7" for
# maximum normal hours, which in 2026 is Prohibition on Further Negotiation. In
# THIS agreement clause 7 IS the working time clause, so the cross-reference was
# correct when it was written and became wrong when clause 5 was inserted and
# nobody updated the definition. A leftover, not a typo.
#
# EVERY FIGURE IS THE SAME AS THE 2026 AGREEMENT'S. That is a finding, not an
# assumption: each was read off this gazette and then compared. The two
# differences found are both in clause 7.4/8.4's meal interval detail - the 2026
# agreement adds a two-hour interval for shifts of ten hours and above, limited
# to the Health Care and Hospitality sectors, and changes the "longer than one
# hour" trigger to "longer than two hours" for cleaners. Neither figure is
# loaded in either rule set, so neither row differs.
# ---------------------------------------------------------------------------

RULES_OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2023.04.01-bccci-rules.json"
)

DERIVED_LEAVE_DAYS = (
    "DERIVED, not transcribed, by the same arithmetic the BCEA itself uses: clause 8.1(a)'s "
    "21 CONSECUTIVE days is three weeks, which is 15 working days on a five-day week and 18 "
    "on a six-day week, and the monthly accrual follows over a 12-month cycle. Clause 8.1(b)'s "
    "28 consecutive days is four weeks, so 20 and 24. The agreement states none of these six "
    "figures directly."
)

FROM_THE_ACT = (
    "NOT FROM THIS AGREEMENT. The agreement is silent, so the BCEA governs and these columns "
    "carry its figures: s20(2)(b)'s one day per 17 worked, s20(4)'s six months, s40(b)'s "
    "payout on termination, s27(1)'s three days of family responsibility leave on four "
    "months' service and four days a week, and s25's four weeks before and six weeks after. "
    "There is no family responsibility clause in this agreement at all - checked through to "
    "clause 17, not assumed from the successor."
)

VESTIGIAL_PARENTAL = (
    "THE THREE parental_leave_* COLUMNS ARE READ BY NOTHING. parental_leave_quantum is the "
    "authoritative table (D-201) and is separately effective-dated from 3 October 2025, the "
    "day the Van Wyk interim reading-in took effect - which matters here because this row "
    "spans that date and these columns cannot express what applied before it. They carry the "
    "same values every other rule set carries so the row can be created at all; do NOT read "
    "them as a statement about the pre-Van Wyk position (O-34)."
)

MEAL_HOUR_IS_THE_ACTS = (
    "meal_interval_after_hours = 5 is clause 7.4's own trigger. meal_interval_minutes = 60 is "
    "NOT: clause 7.4 states no general duration, only that the interval may be reduced to not "
    "less than half an hour by agreement. The hour is BCEA s14(1), which this agreement does "
    "not displace (D-248, the same finding as for the successor)."
)

NO_STANDBY = (
    "ZERO MEANS THE CONCEPT DOES NOT EXIST IN THIS AGREEMENT. Standby is a Sectoral "
    "Determination 7 creature; this agreement has no standby regime, and neither does its "
    "successor."
)

NO_ACCOMMODATION_CAP = (
    "accommodation_deduction_capped = false: clause 5.3(a) permits a deduction for "
    "accommodation with the employee's written consent and states NO percentage, so no cap is "
    "loaded and accommodation_deduction_max_pct is null. That is the ABSENCE of a cap, not a "
    "cap of zero (D-198)."
)

RULES_FIXTURE = {
    "version_label": "REF-2023.04.01-BCCCI-RULES",
    "applies_from": "2023-04-01",
    "description": (
        "Leave and working time rules for contract cleaning Area B under the PREDECESSOR "
        "BCCCI Main Collective Agreement, Notice 1726 of 2023 in GG 48356. Read clause by "
        "clause off the rendered gazette; its clause numbering is one behind the 2026 "
        "agreement's and was not derived from it. Closes on 1 April 2026. NOT verified."
    ),
    "tables": {
        "leave_rule_set": [
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clauses 8, 9 and 11",
                "source_url": GAZETTE_URL,
                "notes": " ".join(
                    [
                        "Clause 8.1(a): 21 consecutive days for an employee working not more "
                        "than 6 days a week, which is the BCEA s20 position. Clause 8.1(b): 28 "
                        "consecutive days for MORE THAN ten years' service with the same "
                        "employer, so ten years to the day stays in the lower band (D-158). "
                        "Clause 9.1 mirrors BCEA s22 exactly: a 36-month cycle (9.1(a)), six "
                        "weeks worth (9.1(b)), one day per 26 days worked in the first six "
                        "months (9.1(c)) and the first-cycle reduction (9.1(d)). Clause 9.3's "
                        "certificate rule is NOT loaded - like its successor it demands one "
                        "after more than ONE consecutive day where BCEA s23(1) says two, which "
                        "s49(1)(e) forbids (D-244, D-248). Clause 11's study leave has no "
                        "column here and is recorded as scope (O-31).",
                        DERIVED_LEAVE_DAYS,
                        FROM_THE_ACT,
                        VESTIGIAL_PARENTAL,
                        NOT_A_GAZETTEER_RULES,
                    ]
                ),
                "annual_leave_days_per_cycle_5day": "15.000",
                "annual_leave_days_per_cycle_6day": "18.000",
                "annual_accrual_days_per_month_5day": "1.250",
                "annual_accrual_days_per_month_6day": "1.500",
                "annual_accrual_ratio_days_worked": 17,
                "annual_accrual_ratio_hours_worked": 17,
                "annual_leave_cycle_months": 12,
                "annual_leave_forfeit_months": 6,
                "annual_leave_payable_on_termination": True,
                "has_long_service_annual_leave": True,
                "long_service_annual_leave_years": 10,
                "long_service_years_inclusive": False,
                "long_service_annual_leave_days_5day": "20.000",
                "long_service_annual_leave_days_6day": "24.000",
                "sick_leave_cycle_months": 36,
                "sick_leave_weeks_equivalent": "6.00",
                "sick_leave_first_six_months_ratio": 26,
                "sick_leave_payable_on_termination": False,
                "family_responsibility_days": 3,
                "family_resp_min_service_months": 4,
                "family_resp_min_days_per_week": 4,
                "parental_leave_total_months": 4,
                "parental_leave_additional_days": 10,
                "parental_leave_shareable": True,
                "maternity_earliest_start_weeks_before_birth": 4,
                "maternity_no_work_weeks_after_birth": 6,
            }
        ],
        "working_time_rule_set": [
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clauses 3, 4.3, 4.6, 7, 10 and 15",
                "source_url": GAZETTE_URL,
                "notes": " ".join(
                    [
                        "Clause 4.3: a night work allowance of 10 percent of the hourly wage "
                        "for each night hour or part thereof, IN ADDITION TO the ordinary "
                        "wage; clause 3 puts the night window at 18:00 to 06:00 for work other "
                        "than overtime. Clause 4.6(a) - NOT a clause 5, which is Payment of "
                        "Remuneration in this agreement: an employer may not employ a cleaner "
                        "for less than 6 hours a day and pays 6 if fewer are worked. Clause "
                        "7.3: 45 hours a week, 9 a day on five days or fewer, 8 a day on more "
                        "than five. Clause 7.5: 3 overtime hours a day, 10 a week, at least "
                        "one and a half times. Clause 7.8: 12 consecutive hours daily rest, 36 "
                        "weekly which need not include Sunday. CLAUSE 10 REPRODUCES BCEA s16 "
                        "AND s18, including 10.1(b)(ii)(ab)'s 'whichever is the greater' and "
                        "10.2(b)'s daily-wage floor on a short Sunday, so Area B priced then "
                        "exactly as it prices now and calculators/gross.py needs no new code "
                        "(D-217). Clause 7.6's compressed week and 7.7's four-month averaging "
                        "have no columns here and are recorded as scope (O-31).",
                        MEAL_HOUR_IS_THE_ACTS,
                        NO_STANDBY,
                        NO_ACCOMMODATION_CAP,
                        NOT_A_GAZETTEER_RULES,
                    ]
                ),
                "ordinary_hours_per_week": "45.00",
                "ordinary_hours_per_day_5day": "9.00",
                "ordinary_hours_per_day_6day": "8.00",
                "overtime_multiplier": "1.500",
                "max_overtime_hours_per_day": "3.00",
                "max_overtime_hours_per_week": "10.00",
                "sunday_multiplier_non_ordinary": "2.000",
                "sunday_multiplier_ordinary": "1.500",
                "public_holiday_worked_multiplier": "2.000",
                "public_holiday_not_worked_paid": True,
                "night_work_start_time": "18:00:00",
                "night_work_end_time": "06:00:00",
                "night_allowance_type": "percentage",
                "night_allowance_value": "10.0000",
                "standby_allowance_per_shift": "0.00",
                "standby_window_start": "00:00:00",
                "standby_window_end": "00:00:00",
                "standby_hours_before_overtime": "0.00",
                "min_paid_hours_per_day": "6.00",
                "meal_interval_after_hours": "5.00",
                "meal_interval_minutes": 60,
                "daily_rest_hours": 12,
                "weekly_rest_hours": 36,
                "accommodation_deduction_capped": False,
            }
        ],
        "statutory_parameter": [
            {
                "parameter_code": "PRENATAL_CLINIC_PAID_DAYS_PER_MONTH",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "1.000000",
                "unit": "days",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 12.2",
                "source_url": GAZETTE_URL,
                "notes": (
                    "One day's FULLY PAID leave in each of the 3 months prior to the expected "
                    "date of confinement, on satisfactory proof of attendance at a prenatal "
                    "clinic. Word for word what the successor's clause 13.2 says. ADDITIVE, "
                    "which is why it is loaded when most of clause 12 is not: BCEA s49(1)(d) "
                    "forbids REDUCING the s25 entitlement, and clause 12.3's twelve-week "
                    "return cap does that and is void to that extent (D-244)."
                ),
            },
            {
                "parameter_code": "PRENATAL_CLINIC_MONTHS_BEFORE_BIRTH",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "3.000000",
                "unit": "months",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 12.2",
                "source_url": GAZETTE_URL,
                "notes": (
                    "The day is given in EACH of the three months, so the entitlement is three "
                    "days in total and one is not carried from one month into the next. Two "
                    "parameters rather than a pre-multiplied three, because the clause states "
                    "two figures and a month with no clinic attended pays nothing."
                ),
            },
            {
                "parameter_code": "MATERNITY_RETURN_PAYMENT_MONTH_DIVISOR",
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "value_numeric": "3.000000",
                "unit": "ratio",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clause 12.4(a)",
                "source_url": GAZETTE_URL,
                "notes": (
                    "On RETURN from maternity leave, one third of one month's wage at the rate "
                    "the employee was on at the time of going on leave - not the rate on "
                    "return. THE DIVISOR IS LOADED, NOT A THIRD: 0,333333 is a rounded figure "
                    "standing where an exact one belongs, and a third of R9 000 is R3 000 "
                    "exactly where 0,333333 x 9000 is R2 999,997 (D-244)."
                ),
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# TERMINATION AND NOTICE. Clause 20 here is clause 21 in the successor and
# clause 35 is clause 36, the same one-behind numbering as everything after
# clause 5 (D-260).
#
# THE NOTICE CONTRADICTION IS INHERITED, NOT INTRODUCED IN 2026 (D-261). This
# agreement's clause 20.1(b) carries the identical defect: two items both
# printed "i)", the second giving two weeks after the first four weeks, and an
# "ii)" giving one week on probation for the period between four weeks and six
# months. Probation is capped at four months by clause 3, so both reach the same
# employee, and clause 20.2(a) prices only the one working day and the two weeks
# - so there is no payment-in-lieu figure for the one-week probation notice
# here either. The middle band is loaded CONTESTED and resolve.notice_band()
# refuses it, exactly as it does for the successor (D-241).
#
# LOADING THIS MAKES MARCH 2026 NOTICE REFUSE WHERE IT USED TO ANSWER. Before,
# Area B fell through to Sectoral Determination 1 and got four weeks. That was a
# confident answer from the wrong instrument. The agreement that actually bound
# those employees gives two answers and settles neither, so refusing is correct
# and answering was not.
# ---------------------------------------------------------------------------

TERMINATION_OUTPUT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "reference"
    / "ref-2023.04.01-bccci-termination.json"
)

CONTESTED = (
    'TWO LIMBS, TWO ANSWERS. The clause says both "Not less than two weeks notice shall '
    'be given after the first four weeks of such employment" and "Not less than one weeks '
    "notice shall be given to an employee whilst on probation, as defined, for the period "
    'of employment between 4 weeks as in sub clause (i) above and six months". Probation '
    "is defined in clause 3 as a maximum of four months, so an employee between four weeks "
    "and six months falls under both. Clause 20.2(a) prices only the one working day and "
    "the two weeks, giving no payment in lieu for the one-week probation notice, so the "
    "payment clause does not settle it either."
)

INHERITED = (
    "The successor carries this defect word for word at its own clause 21.1(b), so it was "
    "not introduced in 2026 - it has been in the gazette since at least March 2023 (D-261)."
)

TERMINATION_FIXTURE = {
    "version_label": "REF-2023.04.01-BCCCI-TERMINATION",
    "applies_from": "2023-04-01",
    "description": (
        "Termination and notice for contract cleaning Area B under the PREDECESSOR BCCCI "
        "Main Collective Agreement, Notice 1726 of 2023 in GG 48356: clause 4.5's December "
        "bonus, clause 35's severance pay and clause 20.1(b)'s three notice bands, the "
        "middle one CONTESTED. Closes on 1 April 2026. NOT verified."
    ),
    "tables": {
        "termination_rule_set": [
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "effective_from": "2023-04-01",
                "effective_to": "2026-04-01",
                "source_reference": f"{GAZETTE}, clauses 4.5, 28, 29 and 35",
                "source_url": GAZETTE_URL,
                "notes": (
                    "Clause 4.5: an annual incentive bonus of 4,33 times the weekly wage, paid "
                    "to all cleaners in employment on 1 December, in December - NOT the 4,333 "
                    "weeks SD1 gives, a different figure in a different instrument that looks "
                    "almost identical. Clause 4.5(b) pro-rates it on full calendar months of "
                    "service divided by 12, unqualified and in force throughout this "
                    "agreement, so a single full month earns a share and the minimum service "
                    "is zero. Clause 4.5(g) makes 4.5(c)(ii), 4.5(d) and 4.5(f) MINIMUMS the "
                    "employer may improve on, which are employer elections in "
                    "employers/onboarding.py and not fixed rules here (D-242) - and this "
                    "agreement letters clause 4.5 CLEANLY, with no duplicate c) and no clause "
                    "4.6 restatement, so the successor's corrupted lettering was introduced in "
                    "2026 (O-32). Clause 35.2: severance of at least one week's remuneration "
                    "per completed year of continuous service, calculated per clause 4, on "
                    "dismissal for operational requirements - the BCEA s41 position. Clause "
                    '35.1 defines operational requirements as the needs "of an employee" '
                    "where BCEA s41(1) says employer, and the successor repeats it, so that "
                    "error is inherited too (O-32). Clause 29: retirement at the state pension "
                    "qualification age - which is why the successor's clause 4.6.1.2 cites "
                    '"section (29)" for retirement and is wrong only because the renumbering '
                    "moved it to 30 (D-260). Clause 28: absence of more than three days "
                    "without satisfactory explanation is desertion."
                ),
                "severance_weeks_per_completed_year": "1.00",
                "severance_requires_operational_reason": True,
                "annual_bonus_weeks": "4.330",
                "annual_bonus_month": 12,
                "annual_bonus_pro_rata_on_termination": True,
                "annual_bonus_min_service_months": 0,
            }
        ],
        "termination_notice_band": [
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "effective_from": "2023-04-01",
                "sequence": 1,
                "source_reference": f"{GAZETTE}, clause 20.1(b)(i)",
                "source_url": GAZETTE_URL,
                "service_from_value": "0",
                "service_from_unit": "weeks",
                "service_from_inclusive": True,
                "service_to_value": "4",
                "service_to_unit": "weeks",
                "service_to_inclusive": True,
                "notice_value": "1",
                "notice_unit": "days",
                "is_contested": False,
                "contested_reason": "",
                "notes": (
                    '"DURING the first four weeks" - the boundary belongs to this band, read '
                    "off the clause's own wording (D-158). One WORKING day, so the unit is "
                    "days and nothing converts it to a fraction of a week: contract cleaning "
                    "runs six-day weeks and a day is worth a sixth of one, not a fifth (D-68). "
                    "Clause 20.2(a)(i) prices it at the daily wage being received at the time "
                    "of termination."
                ),
            },
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "effective_from": "2023-04-01",
                "sequence": 2,
                "source_reference": (
                    f"{GAZETTE}, clause 20.1(b), second 'i)' and 'ii)' as published"
                ),
                "source_url": GAZETTE_URL,
                "service_from_value": "4",
                "service_from_unit": "weeks",
                "service_from_inclusive": False,
                "service_to_value": "6",
                "service_to_unit": "months",
                "service_to_inclusive": True,
                "notice_value": None,
                "notice_unit": "",
                "is_contested": True,
                "contested_reason": CONTESTED,
                "notes": (
                    "Loaded as a band so the range is covered and the contradiction is "
                    "visible, with no value because inventing one would put a period nobody "
                    "can stand behind where a gazetted one belongs (D-241). " + INHERITED
                ),
            },
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_B",
                "effective_from": "2023-04-01",
                "sequence": 3,
                "source_reference": f"{GAZETTE}, clause 20.1(b), second 'i)' as published",
                "source_url": GAZETTE_URL,
                "service_from_value": "6",
                "service_from_unit": "months",
                "service_from_inclusive": False,
                "service_to_value": None,
                "service_to_unit": "",
                "service_to_inclusive": None,
                "notice_value": "2",
                "notice_unit": "weeks",
                "is_contested": False,
                "contested_reason": "",
                "notes": (
                    "Unambiguous from six months on: the one-week rule is expressly limited to "
                    "the window 'between 4 weeks ... and six months' and probation is capped "
                    "at four months, so only the two-week rule can reach an employee of longer "
                    "service. Clause 20.2(a)(ii) prices it at double the weekly wage being "
                    "received at the time of termination, and in doing so calls the two-week "
                    "rule \"20.1 b) ii)\" - which is how we know the printed 'ii)' is a "
                    "mis-numbered third item rather than the second."
                ),
            },
        ],
    },
}

LEAVE_TYPE_FILENAME = "ref-2023.04.01-bccci-leave-types.json"
LEAVE_TYPE_LABEL = "REF-2023.04.01-BCCCI-LEAVE-TYPES"
LEAVE_TYPE_FROM = "2023-04-01"
LEAVE_TYPE_TO = "2026-04-01"
STUDY_CLAUSE = "11"
STEWARD_CLAUSE = "19"
LEAVE_TYPE_DESCRIPTION = (
    "Study leave (clause 11) and shop steward leave (clause 19.4) for contract "
    "cleaning Area B under the PREDECESSOR BCCCI Main Collective Agreement, Notice "
    "1726 of 2023 in GG 48356 - one clause behind the successor throughout (D-260), "
    "and word for word identical in substance. Closes on 1 April 2026. NOT verified."
)


# ---------------------------------------------------------------------------
# STUDY LEAVE AND SHOP STEWARD LEAVE (D-268) - two entitlements this agreement
# CREATES, which the BCEA does not have at all. The prenatal clinic day is the
# third of them and its figures already went in with the maternity benefits
# (D-244), so they are not repeated here.
#
# Their own version rather than an addition to the rule set fixture, which is
# already loaded and checksummed: --supersede permits prose changes only
# (D-199), and a new figure is an ordinary load.
#
# NEITHER IS A PER-CYCLE BANK, which is why both are statutory_parameter rows
# and not leave_rule_set columns. Study leave is per EXAMINATION; shop steward
# leave is per YEAR but at one of two figures depending on a fact about the
# person that no column carries.
# ---------------------------------------------------------------------------

PREPARE_NOTE = (
    "One day's leave to PREPARE for each examination, on full pay. Per EXAMINATION "
    "and not per cycle: the clause gives the entitlement each time an employee "
    "writes, so there is no annual bank for the accrual engine to add to. "
    "Conditioned on satisfactory proof that the employee was allowed to write AND "
    "HAS DULY WRITTEN an examination conducted by a registered educational body; a "
    "casual employee is excluded by name. The clause also lets an employer refuse a "
    "later grant to an employee who already took study leave and failed that "
    "examination - a condition, not a figure, so it is not loaded."
)

WRITE_NOTE = (
    "One day's leave to WRITE each examination, on full pay. Loaded as its own "
    "figure rather than summed with the preparation day, because the clause states "
    "two separate entitlements and an employee who writes without preparing is owed "
    "the second and not the first. Summing them here would make one day's leave "
    "indistinguishable from two."
)

OFFICE_BEARER_NOTE = (
    "4 days' paid leave a year for AN OFFICE BEARER of a representative trade "
    "union, to attend to union affairs. THE OFFICE BEARER GETS FEWER DAYS than an "
    "ordinary shop steward, who gets 6 - which reads like the two limbs were "
    "swapped in drafting. Both agreements print it this way, word for word, three "
    "years apart, so it is transcribed as printed and flagged rather than "
    "corrected: O-32's rule is that nothing here edits a gazette into agreement "
    "with what it ought to say."
)

OTHER_STEWARD_NOTE = (
    "6 days' paid leave a year for ANY OTHER shop steward. Which of the two figures "
    "applies is a fact about the PERSON - whether they are an office bearer of a "
    "representative trade union - and no column on employee carries it, so "
    "statutory.resolve.shop_steward_leave_days() takes the status as an argument "
    "rather than guessing (D-110's shape). The union must give 14 days' written "
    "notice except in an emergency, which is a condition rather than a figure."
)


def _leave_type_parameters(study_clause: str, steward_clause: str) -> list[dict]:
    """The four figures, identical in both agreements, cited to each one's own
    clause numbers (D-260: the predecessor's numbering is one behind)."""
    specifications = (
        ("STUDY_LEAVE_PREPARE_DAYS_PER_EXAM", "1.000000", f"{study_clause}.1(a)", PREPARE_NOTE),
        ("STUDY_LEAVE_WRITE_DAYS_PER_EXAM", "1.000000", f"{study_clause}.1(b)", WRITE_NOTE),
        (
            "SHOP_STEWARD_LEAVE_DAYS_OFFICE_BEARER",
            "4.000000",
            f"{steward_clause}.4(a)(i)",
            OFFICE_BEARER_NOTE,
        ),
        (
            "SHOP_STEWARD_LEAVE_DAYS_OTHER",
            "6.000000",
            f"{steward_clause}.4(a)(ii)",
            OTHER_STEWARD_NOTE,
        ),
    )
    return [
        {
            "parameter_code": code,
            "value_numeric": value,
            "unit": "days",
            "sector": "CONTRACT_CLEANING",
            "sector_area": "AREA_B",
            "effective_from": LEAVE_TYPE_FROM,
            "effective_to": LEAVE_TYPE_TO,
            "source_reference": f"{GAZETTE}, clause {pinpoint}",
            "source_url": GAZETTE_URL,
            "notes": note,
        }
        for code, value, pinpoint, note in specifications
    ]


LEAVE_TYPE_OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / LEAVE_TYPE_FILENAME

LEAVE_TYPE_FIXTURE = {
    "version_label": LEAVE_TYPE_LABEL,
    "applies_from": LEAVE_TYPE_FROM,
    "description": LEAVE_TYPE_DESCRIPTION,
    "tables": {"statutory_parameter": _leave_type_parameters(STUDY_CLAUSE, STEWARD_CLAUSE)},
}


def main():
    for path, document in (
        (OUTPUT, FIXTURE),
        (RULES_OUTPUT, RULES_FIXTURE),
        (TERMINATION_OUTPUT, TERMINATION_FIXTURE),
        (LEAVE_TYPE_OUTPUT, LEAVE_TYPE_FIXTURE),
    ):
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {path.relative_to(path.parents[1])}")


if __name__ == "__main__":
    main()
