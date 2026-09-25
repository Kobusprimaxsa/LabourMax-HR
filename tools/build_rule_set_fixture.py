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
import pathlib
import sys

BCEA = "Basic Conditions of Employment Act 75 of 1997"
BCEA_SUMMARY_URL = "https://lrs.org.za/wp-content/uploads/2024/05/Summary-of-the-BCEA.pdf"

SD7 = (
    "Sectoral Determination 7: Domestic Worker Sector, GN R.1068 in RG 7434 "
    "(GG 23732), 15 August 2002"
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


#: D-320. Written onto both BCCCI rows so nobody 'corrects' it back.
ELECTION = (
    "WHY A KWAZULU-NATAL LEAVER IS PAID: clause 4.5 pays the bonus 'to all cleaners "
    "in employment on the 1st December' and has NO termination limb - unlike SD1 "
    "clause 3(3), which pays 'during the month of December or on termination of "
    "employment'. annual_bonus_pro_rata_on_termination is TRUE on this row anyway, by"
    " Kobus's deliberate election on 25 September 2026 (D-320): a leaver is paid pro "
    "rata on the COMPLETED FULL CALENDAR MONTHS of the current cycle. That is MORE "
    "generous than the agreement requires, and lawful, because the agreement sets a "
    "minimum. Do not 'correct' this to the literal clause: that would quietly stop "
    "paying leavers."
)

DERIVED_LONG_SERVICE_DAYS = (
    "DERIVED the same way the 15 and the 18 above are, and by the same arithmetic "
    "the BCEA itself uses: 28 CONSECUTIVE days is four weeks, which is 20 working "
    "days on a five-day week and 24 on a six-day week - exactly as 21 consecutive "
    "days is three weeks, 15 and 18. Nothing new is read into the clause."
)


def leave_rules(
    *,
    sector,
    source,
    source_url="",
    family_days,
    sector_area=None,
    effective_from=EFFECTIVE_FROM,
    extra_notes="",
    long_service_years=None,
    long_service_years_inclusive=None,
    long_service_days_5day=None,
    long_service_days_6day=None,
):
    notes = [EFFECTIVE_NOTE, DERIVED_LEAVE_DAYS]
    if long_service_years is not None:
        notes.append(DERIVED_LONG_SERVICE_DAYS)
    if extra_notes:
        notes.append(extra_notes)
    row = {
        "effective_from": effective_from,
        "source_reference": source,
        "source_url": source_url,
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
        # NOT NULL with no default (D-243). FALSE is a statement about what this
        # instrument says, carried by every row that does not pass a band.
        "has_long_service_annual_leave": long_service_years is not None,
        "long_service_annual_leave_years": long_service_years,
        "long_service_years_inclusive": long_service_years_inclusive,
        "long_service_annual_leave_days_5day": long_service_days_5day,
        "long_service_annual_leave_days_6day": long_service_days_6day,
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
    if sector_area:
        row["sector_area"] = sector_area
    return row


def working_time_rules(
    *,
    sector,
    source,
    source_url="",
    sector_area=None,
    effective_from=EFFECTIVE_FROM,
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
        "effective_from": effective_from,
        "source_reference": source,
        "source_url": source_url,
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
    if sector_area:
        row["sector_area"] = sector_area
    return row


def termination_rules(
    *,
    sector,
    source,
    source_url="",
    sector_area=None,
    effective_from=EFFECTIVE_FROM,
    bonus_weeks="0.000",
    bonus_month=None,
    bonus_pro_rata=False,
    extra_notes="",
    bonus_note=None,
):
    """Severance and pro-rata bonus only. Notice moved to
    ``termination_notice_band`` (D-68) — see ``build_notice_band_fixture.py``.
    """
    notes = [EFFECTIVE_NOTE]
    # A BCCCI row describes clause 4.5 in its OWN terms (D-320): SD1's sentence on
    # a BCCCI row was how SD1's 4,333 and termination limb came to be read there.
    if bonus_month is None:
        notes.append(NO_STATUTORY_BONUS)
    else:
        notes.append(bonus_note or HAS_STATUTORY_BONUS)
    if extra_notes:
        notes.append(extra_notes)
    row = {
        "effective_from": effective_from,
        "source_reference": source,
        "source_url": source_url,
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
    if sector_area:
        row["sector_area"] = sector_area
    return row


DOCUMENT = {
    "version_label": "REF-2026.03.01-RULES-r5",
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
                source_url=SD7_URL,
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
                source_url=SD7_URL,
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
                source_url=SD7_URL,
            ),
        ],
    },
}


SD1 = (
    "Sectoral Determination 1: Contract Cleaning Sector, current consolidated text "
    "(clauses 3, 8-24)"
)
SD1_URL = "https://www.acts.co.za/basic/sd1_nr622_3__remuneration.php"

#: THE CITATION MUST CONTAIN EVERY FIGURE THE ROW CARRIES (D-279). The first
#: encoding named clauses 18, 19 and 22 — annual, sick and family
#: responsibility — and the row also carries two MATERNITY figures, which are
#: clause 20. A verifier opening 18, 19 and 22 would have found three of the
#: row's figures absent from the clauses they were sent to, and the honest
#: outcomes are both bad: tick anyway, or spend the evening deciding the load
#: is wrong when only the pinpoint is.
#:
#: The parenthesised subject after each number is not decoration. The workbook
#: prints ONE row for a group of figures and the person has to know which
#: clause to open for which figure; "clauses 18, 19 and 22" made them guess.
#:
#: "parental: BCEA s25" is the other half of the correction and is a POINTER
#: AWAY from this instrument. SD1 states no parental leave at all — the three
#: parental_leave_* columns carry the Van Wyk interim reading-in of BCEA s25,
#: which is not in any clause of any sectoral determination. Saying so in the
#: citation is the only honest thing available while those columns exist;
#: whether they should exist is O-39.
SD1_LEAVE_CLAUSES = (
    ", clauses 18 (annual), 19 (sick), 20 (maternity), 22 (family responsibility)"
    "; parental: BCEA s25"
)

DOCUMENT_SD1 = {
    "version_label": "REF-2026.03.01-SD1-r5",
    "applies_from": "2026-03-01",
    "description": (
        "Contract cleaning sector rule sets from Sectoral Determination 1, clauses 3 "
        "and 8 to 24. Researched 13 September 2026 from two independent transcriptions "
        "of the consolidated determination. The leave row's citation now names the "
        "clause for each figure it carries, maternity's clause 20 included (D-279). "
        "NOT verified."
    ),
    "tables": {
        "leave_rule_set": [
            leave_rules(
                sector="CONTRACT_CLEANING",
                source=f"{SD1}{SD1_LEAVE_CLAUSES}",
                family_days=3,
                extra_notes=(
                    f"Annual leave, sick leave and family responsibility leave all match "
                    f"the BCEA - SD1 adds nothing here, unlike SD7 which gives five "
                    f"family responsibility days. Parental leave is the same interim "
                    f"position: {ZACC} "
                    f"CLAUSE NUMBERING, from the determination itself: 18 annual leave "
                    f"(18(6) is the payout on termination), 19 sick leave, 20 maternity "
                    f"(20(2) four weeks before the birth, 20(3) six weeks after), 22 "
                    f"family responsibility (22(1) the two eligibility limbs, 22(2) the "
                    f"three days). The same numbering appears in the determination as "
                    f"published in 1999 and in the consolidation as at 1 March 2026 "
                    f"(D-194, D-279). "
                    f"THE THREE parental_leave_* COLUMNS ARE NOT IN THIS INSTRUMENT and "
                    f"no clause can be cited for them: they carry the Van Wyk interim "
                    f"reading-in of BCEA s25. Nothing reads them - the live source is "
                    f"parental_leave_quantum, which is effective-dated to end when the "
                    f"suspension does on 3 October 2028 (D-203), while these columns "
                    f"carry no end date at all. O-39."
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


# ---------------------------------------------------------------------------
# The BCCCI Main Collective Agreement (KwaZulu-Natal), GN R.7296 in GG 54412,
# 27 March 2026. Contract cleaning AREA B ONLY - SD1 still governs Areas A and
# C of the same sector, which is why these rows carry an area and SD1's do not
# (D-240). Every figure verified against the gazette page by page.
#
# What is deliberately NOT here, and why:
#
# * Maternity, clause 13.3 and clause 17(b). BCEA s49(1)(d) forbids a
#   bargaining council agreement from reducing the s25 entitlement, and 13.3
#   caps the return at twelve weeks after the birth where an employee who works
#   to the birth is entitled to roughly seventeen and a half. Void to that
#   extent, so it is transcribed in the register and NOT loaded as a rule.
# * The clause 10.3(a)(i) certificate rule. It requires a certificate after
#   "more than one consecutive day" where BCEA s23(1) says more than two -
#   caught by s49(1)(e). NOTE, corrected in chunk C against the gazette: cl
#   10.2(a) does NOT track s23(1) and does NOT contradict 10.3(a)(i). It
#   states the SAME one-day threshold, and additionally drops a word - "An
#   employer IS required to pay ... if the employee has been absent from work
#   for one day", where s23(1) says an employer is NOT required to pay where
#   the employee has been absent more than two consecutive days. So the
#   agreement is internally consistent on one day and departs from the Act in
#   both places; the reduction is void on that ground alone (D-248).
#   SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS stays at 2.
# * Notice between four weeks and six months. Clause 21.1(b) gives two answers
#   and nothing resolves them.
# ---------------------------------------------------------------------------

# O-33: one instrument, one citation. This agreement was cited two ways -
# the council's full name with 'Government Gazette' spelled out on the wage
# and rule set rows, and a short 'BCCCI ... GG' form on the notice bands,
# where the long one plus a pinpoint would not fit source_reference's 200
# characters. The verification workbook groups by source document, so the
# two spellings read as two documents and a person could verify one to
# completion with the version still showing incomplete (D-257). This form
# keeps the council's full name, abbreviates only the province, and leaves
# room for the longest pinpoint in the set.
BCCCI = (
    "Bargaining Council for the Contract Cleaning Services Industry (KZN) "
    "Main Collective Agreement, GN R.7296 in GG 54412, 27 March 2026"
)
BCCCI_FROM = "2026-04-01"

DOCUMENT_BCCCI = {
    "version_label": "REF-2026.04.01-BCCCI-RULES-r4",
    "applies_from": BCCCI_FROM,
    "description": (
        "BCCCI (KwaZulu-Natal) Main Collective Agreement rule sets for contract "
        "cleaning Area B. Transcribed from GN R.7296 in GG 54412 page by page. "
        "NOT verified."
    ),
    "tables": {
        "leave_rule_set": [
            leave_rules(
                sector="CONTRACT_CLEANING",
                sector_area="AREA_B",
                effective_from=BCCCI_FROM,
                source=f"{BCCCI}, clauses 9, 10 and 12",
                family_days=3,
                long_service_years=10,
                # "MORE THAN ten years": ten years exactly stays in the LOWER
                # band, read off the clause's own wording (D-158).
                long_service_years_inclusive=False,
                long_service_days_5day="20.000",
                long_service_days_6day="24.000",
                extra_notes=(
                    "Clause 9.1(a) gives 21 consecutive days for an employee working not "
                    "more than 6 days a week, which is the BCEA s20 position. Clause "
                    "9.1(b) gives 28 CONSECUTIVE days to an employee with MORE THAN ten "
                    "years service, loaded into the long_service columns (D-243). "
                    "Clause 10's sick leave QUANTUM mirrors BCEA s22 exactly: a 36-month "
                    "cycle (cl 10.1(a)), six weeks worth (cl 10.1(b)), one day per 26 days "
                    "worked in the first six months (cl 10.1(c)) and the first-cycle "
                    "reduction (cl 10.1(d)). The clause 10.3(a)(i) CERTIFICATE rule is not "
                    "loaded - it demands a certificate after more than ONE consecutive day "
                    "where BCEA s23(1) says two, which s49(1)(e) forbids and which cl "
                    "10.2(a) states the same one-day threshold rather than contradicting it "
                    "(D-248, corrected in chunk C). Clause 9.2(a) requires annual "
                    "leave to COMMENCE within three months of the cycle ending, "
                    "extendable by a further three by written agreement; "
                    "annual_leave_forfeit_months carries the BCEA s20(4) six months "
                    "because that is the outer limit both instruments reach, and "
                    "clause 9.2(a)'s stricter timing duty has no column and is not "
                    "modelled. Clause 9.1 excludes CASUAL employees from annual leave "
                    "entirely, which is also not modelled. Family responsibility "
                    "follows the BCEA three days; the agreement adds nothing. Clause 12's "
                    "study leave has no column here and is recorded as scope."
                ),
            )
        ],
        "working_time_rule_set": [
            working_time_rules(
                sector="CONTRACT_CLEANING",
                sector_area="AREA_B",
                effective_from=BCCCI_FROM,
                source=f"{BCCCI}, clauses 3, 4.3, 5, 8, 11, 16 and 17",
                max_overtime_week="10.00",
                standby_allowance="0.00",
                standby_start="00:00:00",
                standby_end="00:00:00",
                standby_hours="0.00",
                accommodation_capped=False,
                accommodation_source="the BCCCI Main Collective Agreement",
                night_allowance_type="percentage",
                night_allowance_value="10.0000",
                min_paid_hours="6.00",
                extra_notes=(
                    "Clause 4.3: a night work allowance of 10 percent of the employee's "
                    "hourly wage for each night hour or part thereof, IN ADDITION TO the "
                    "ordinary wage; clause 3 puts the night window at 18:00 to 06:00 for "
                    "work other than overtime. Clause 5: an employer may not employ a "
                    "cleaner for less than 6 hours a day and pays 6 if fewer are worked. "
                    "Clause 8.3: 45 hours a week, 9 a day on five days or fewer, 8 a day "
                    "on more than five. Clause 8.5: 3 overtime hours a day, 10 a week, at "
                    "least one and a half times. Clause 8.8: 12 consecutive hours daily "
                    "rest, 10 for an employee living on the premises whose meal interval "
                    "is at least three hours; 36 weekly, need not include Sunday; or 60 "
                    "every two weeks. Clause 8.4: no more than five hours continuous work "
                    "without a meal interval - the FIVE is the agreement's. "
                    "meal_interval_minutes = 60 is NOT: clause 8.4 states no general "
                    "duration, only that it may be reduced to not less than half an "
                    "hour by agreement, and its one-hour/two-hour rule in 8.4(b) is "
                    "expressly limited by 8.4(b)(i) to the Health Care and Hospitality "
                    "sectors. The hour is BCEA s14(1), which this agreement does not "
                    "displace (D-248). "
                    "without a meal interval. CLAUSE 11 REPRODUCES BCEA s16 AND s18 ALMOST "
                    "VERBATIM, including 11.1(b)(ii) 'whichever is the greater' and "
                    "11.2(b)'s daily-wage floor on a short Sunday, so Area B prices exactly "
                    "as the BCEA does and calculators/gross.py needs no new code (D-217). "
                    "Standby and accommodation are SD7 concepts; this agreement has "
                    "neither. Clause 8.6's compressed week and 8.7's four-month averaging "
                    "have no columns here and are recorded as scope."
                ),
            )
        ],
        "termination_rule_set": [
            termination_rules(
                sector="CONTRACT_CLEANING",
                sector_area="AREA_B",
                effective_from=BCCCI_FROM,
                source=f"{BCCCI}, clauses 4.5, 29, 30 and 36",
                bonus_weeks="4.330",
                bonus_month=12,
                bonus_pro_rata=True,
                bonus_note=(
                    "BCCCI clause 4.5: an annual incentive bonus of 4,33 (four point three "
                    "three) times the employee's weekly wage, paid to all cleaners in "
                    "employment on 1 December, in December, no later than the 20th. THE "
                    "MULTIPLIER IS 4.33, NOT 4.333: SD1's annual_bonus_weeks is 4.333, and the "
                    "two are different figures in different instruments that look almost "
                    "identical. Clause 4.5(b) pro-rates it on full calendar months of service "
                    "divided by 12, so a single full month earns a share and the minimum "
                    "service is zero - and PRO RATA IS IN FORCE NOW: clause 4.6 is a "
                    "re-lettered restatement of 4.5 whose preamble says its inclusion 'will "
                    "come into effect in the increase year of 2028'; no figure differs, so "
                    "nothing here changes in 2028 but the clause to cite (D-248). 4.5(g) makes "
                    "4.5(c)(ii), 4.5(d) and 4.5(f) MINIMUMS the employer may improve on, so all "
                    "three are employer elections in employers/onboarding.py with the gazetted "
                    "position as the default (D-242), never fixed rules here. " + ELECTION
                ),
                extra_notes=(
                    "Clause 4.5(f): casual employees do not qualify. Clause 36.2: severance "
                    "of at least one week's remuneration per completed year of continuous "
                    "service, calculated per clause 4 - the BCEA s41 position. Clause 30: "
                    "retirement at the state pension qualification age. Clause 29: absence "
                    "of more than three days without satisfactory explanation is desertion. "
                    "NOT MODELLED HERE: clause 4.5(d)'s prevailing-rate split beyond the "
                    "election, 4.5(e)'s absence penalties."
                ),
            )
        ],
    },
}

# ---------------------------------------------------------------------------
# WRITES the file rather than printing it for redirection (D-264). A shell
# redirect re-encodes: PowerShell's `>` writes the console code page, and
# `python tools/build_notice_band_fixture.py > reference/...json` put U+FFFD
# REPLACEMENT CHARACTER through every non-ASCII character in that file - the
# rand sign and the en dashes in the notes. Nothing refused it: the JSON was
# still valid, the loader read it, and the corruption reached the verification
# workbook as the mojibake a person would have to decide about. Writing with an
# explicit encoding cannot be got wrong from the calling shell.
# ---------------------------------------------------------------------------


REFERENCE = pathlib.Path(__file__).resolve().parents[1] / "reference"

#: Which document goes in which file. A dict rather than an if/else because the
#: failure being prevented is "somebody adds a third document and redirects it
#: at the wrong name", and a mapping makes the pairing the thing you edit.
OUTPUTS = {
    "bcea": ("ref-2026.03.01-rules.json", DOCUMENT),
    "sd1": ("ref-2026.03.01-sd1.json", DOCUMENT_SD1),
    "bccci": ("ref-2026.04.01-bccci-rules.json", DOCUMENT_BCCCI),
}


def main(argv=None):
    which = (argv or sys.argv[1:] or ["bcea"])[0]
    if which not in OUTPUTS:
        raise SystemExit(f"Unknown document {which!r}. Choose one of {', '.join(OUTPUTS)}.")
    filename, document = OUTPUTS[which]
    path = REFERENCE / filename
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {path.name}")


if __name__ == "__main__":
    main()
