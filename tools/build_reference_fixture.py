"""Builds the 1 March 2026 statutory reference fixture.

The public holiday rows are generated rather than typed, because the Public
Holidays Act 36 of 1994 gives ten fixed dates plus two that move with Easter, and
s2(1) ADDS the following Monday whenever one of them falls on a Sunday — it
does not move the holiday off the Sunday, which is why a Sunday holiday emits
TWO rows (D-280). Typing twenty-eight dates by hand is how a year ends up
missing Family Day.

Everything else is transcribed from the source named in that row's
``source_reference``. **No figure in this file was calculated, inferred or filled
in from memory.** Where a figure could not be found in a primary source it is
absent from the fixture rather than estimated — see the KwaZulu-Natal contract
cleaning rates, which the gazette deliberately does not state.

Run: python tools/build_reference_fixture.py > reference/ref-2026.03.01.json
"""

from __future__ import annotations

import datetime
import json
import pathlib

NMW_GAZETTE = "GN R.7083, GG 54075, 3 February 2026 (National Minimum Wage Act 9 of 2018)"
NMW_URL = "https://www.gov.za/sites/default/files/gcis_document/202602/54075rg11941gon7083.pdf"

SARS_2027 = "SARS Rates of Tax for Individuals, 2027 tax year (1 March 2026 - 28 February 2027)"
SARS_URL = "https://www.sars.gov.za/tax-rates/income-tax/rates-of-tax-for-individuals/"

# Every one of these was fetched and the document confirmed to be the one cited
# (D-274). A citation whose reader cannot reach the document is doing half its
# job, and a link to the WRONG document is worse than none.
HOLIDAYS_URL = "https://www.gov.za/sites/default/files/gcis_document/201409/act36of1994.pdf"
UIC_ACT_URL = "https://www.gov.za/sites/default/files/gcis_document/201409/a4-02.pdf"
#: Notice 475 in GG 44641. The body of this one is an image, so the page carries
#: no searchable text - it is still the gazette the citation names.
UIF_CEILING_URL = "https://www.gov.za/sites/default/files/gcis_document/202105/44641gon475.pdf"
SDL_ACT_URL = "https://www.gov.za/sites/default/files/gcis_document/201409/a9-99.pdf"
VAT_ACT_URL = "https://www.gov.za/sites/default/files/gcis_document/201505/act-89-1991s.pdf"
COIDA_URL = "https://www.gov.za/sites/default/files/gcis_document/202604/54577gen3910.pdf"
THRESHOLD_2025_URL = (
    "https://www.gov.za/sites/default/files/gcis_document/202503/52232rg11806gon5970.pdf"
)
#: The Department's own copy. gov.za serves this gazette under a path that does
#: not follow the usual pattern, and labour.gov.za publishes the notice itself.
THRESHOLD_2026_URL = (
    "https://www.labour.gov.za/DocumentCenter/Regulations%20and%20Notices/Notices/"
    "Basic%20Conditions%20of%20Employment/"
    "Basic%20Conditions%20of%20Employment%20Act_Determination%20Earnings%20Threshold2026.pdf"
)


HOLIDAYS_ACT = "Public Holidays Act 36 of 1994, Schedule 1"
HOLIDAYS_SHIFT = "Public Holidays Act 36 of 1994, s2(1)"

FIXED_HOLIDAYS = [
    (1, 1, "New Year's Day"),
    (3, 21, "Human Rights Day"),
    (4, 27, "Freedom Day"),
    (5, 1, "Workers' Day"),
    (6, 16, "Youth Day"),
    (8, 9, "National Women's Day"),
    (9, 24, "Heritage Day"),
    (12, 16, "Day of Reconciliation"),
    (12, 25, "Christmas Day"),
    (12, 26, "Day of Goodwill"),
]


def easter_sunday(year: int) -> datetime.date:
    """Anonymous Gregorian computus. Good Friday and Family Day hang off this."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    the_l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * the_l) // 451
    month, day = divmod(h + the_l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def public_holidays(year: int) -> list[dict]:
    """Every public holiday in a calendar year — ONE ROW PER DAY THAT IS ONE.

    **s2(1) ADDS the Monday. It does not move the holiday off the Sunday.**
    "The days mentioned in Schedule 1 shall be public holidays, and whenever any
    public holiday falls on a Sunday, the following Monday shall be a public
    holiday." Schedule 1 fixes the date — 9 August is National Women's Day —
    and nothing in s2(1) takes that away when the date lands on a Sunday. So a
    Sunday holiday produces TWO rows, which is what gov.za's own calendar
    lists: 9 AND 10 August 2026, 21 AND 22 March 2027, 26 AND 27 December 2027.

    **This function used to emit ONE row, dated the Monday** (D-280), with
    ``shifted_from_date`` pointing back at a Sunday that was never loaded. So
    ``resolve.is_public_holiday()`` was False on the Sunday: an employee who
    worked it was paid the s16 Sunday rate and not the s18 public holiday
    rate, and a salaried employee who did not work it lost the s18(2)(a) paid
    day. Contract cleaning works Sundays routinely, so that was a live
    underpayment rather than a cosmetic data point.

    ``shifted_from_date`` stays on the Monday. It is still true and still the
    only thing that distinguishes a Monday the Act added from a Monday
    Schedule 1 names in its own right (Family Day).
    """
    easter = easter_sunday(year)
    days = [(datetime.date(year, m, d), name) for m, d, name in FIXED_HOLIDAYS]
    days.append((easter - datetime.timedelta(days=2), "Good Friday"))
    days.append((easter + datetime.timedelta(days=1), "Family Day"))

    rows = []
    for day, name in sorted(days):
        # Good Friday is always a Friday and Family Day always a Monday, so only
        # the fixed-date holidays can ever land on a Sunday.
        is_sunday = day.weekday() == 6
        rows.append(
            {
                "holiday_date": str(day),
                "name": name,
                "source_reference": HOLIDAYS_ACT,
                "source_url": HOLIDAYS_URL,
                **(
                    {
                        "notes": (
                            f"{name} falls on Sunday {day:%d %B %Y}. Schedule 1 fixes the "
                            f"date, so this day is a public holiday in its own right; "
                            f"s2(1) ADDS the following Monday as a second one. Both are "
                            f"public holidays and gov.za lists both."
                        )
                    }
                    if is_sunday
                    else {}
                ),
            }
        )
        if is_sunday:
            monday = day + datetime.timedelta(days=1)
            rows.append(
                {
                    "holiday_date": str(monday),
                    "name": name,
                    "shifted_from_date": str(day),
                    "source_reference": HOLIDAYS_SHIFT,
                    "source_url": HOLIDAYS_URL,
                    "notes": (
                        f"{name} fell on Sunday {day:%d %B %Y}, so s2(1) makes this "
                        f"Monday a public holiday as well. The Sunday keeps its own "
                        f"row — the Act adds a day, it does not move one."
                    ),
                }
            )
    return rows


BRACKET_NOTE = (
    "SARS prints this band's lower bound one rand higher. It is stored as the "
    "'taxable income above R' figure the calculation actually uses, so no rand falls "
    "between two bands. The two readings agree at the boundary by construction: the "
    "previous band's tax at its ceiling equals this band's base."
)

PAYE_BRACKETS = [
    # (order, income_from, income_to, base_tax, marginal rate) - SARS 2027 tax year.
    (1, "0.00", "245100.00", "0.00", "18.000"),
    (2, "245100.00", "383100.00", "44118.00", "26.000"),
    (3, "383100.00", "530200.00", "79998.00", "31.000"),
    (4, "530200.00", "695800.00", "125599.00", "36.000"),
    (5, "695800.00", "887000.00", "185215.00", "39.000"),
    (6, "887000.00", "1878600.00", "259783.00", "41.000"),
    (7, "1878600.00", None, "666339.00", "45.000"),
]


def brackets() -> list[dict]:
    rows = []
    for order, low, high, base, rate in PAYE_BRACKETS:
        row = {
            "tax_year": "2026/2027",
            "bracket_order": order,
            "income_from": low,
            "income_to": high,
            "base_tax": base,
            "marginal_rate_pct": rate,
            "source_reference": SARS_2027,
            "source_url": SARS_URL,
        }
        if order > 1:
            row["notes"] = BRACKET_NOTE
        rows.append(row)
    return rows


DOCUMENT = {
    # -r2: the two UIF citations corrected (D-211). Same figures, so it travels
    # through --supersede (D-199) rather than as a new load.
    # -r4: the three Sunday holiday NOTES corrected (D-280). The Sunday ROWS
    # themselves are new data and arrive in ref-2026.03.01-holidays.json, which
    # loads before this supersede does - a re-encoding may not add a row, and
    # this one does not: every row here already exists by the time it runs.
    "version_label": "REF-2026.03.01-r4",
    "applies_from": "2026-03-01",
    "description": (
        "First statutory reference load: the 1 March 2026 wage floors, the SARS 2027 "
        "tax year tables, the contribution parameters, and public holidays for 2026 "
        "and 2027. Researched from primary sources on 13 September 2026. NOT verified - "
        "every figure must be checked against the source document before this version "
        "can come into force."
    ),
    "tables": {
        "sector": [
            {
                "code": "DOMESTIC",
                "name": "Domestic worker sector",
                "determination_reference": "Sectoral Determination 7",
                "uses_area_rates": False,
                "has_statutory_annual_bonus": False,
                "has_provident_fund": False,
            },
            {
                "code": "CONTRACT_CLEANING",
                "name": "Contract cleaning sector",
                "determination_reference": "Sectoral Determination 1",
                "uses_area_rates": True,
                "has_statutory_annual_bonus": True,
                "has_provident_fund": True,
            },
        ],
        "sector_area": [
            {
                "sector": "CONTRACT_CLEANING",
                "code": "AREA_A",
                "name": "Area A - listed Metropolitan and Local Councils",
                "description": (
                    "Metropolitan Councils: City of Cape Town, Greater East Rand Metro, City of "
                    "Johannesburg, Tshwane and Nelson Mandela. Local Council: Emfuleni, Merafong, "
                    "Mogale City, Metsimaholo, Randfontein, Stellenbosch, Westonaria. The gazette "
                    "prints the metros and the local councils in ONE column, not two - see "
                    "municipality_area_map for the list as loaded."
                ),
            },
            {
                "sector": "CONTRACT_CLEANING",
                "code": "AREA_B",
                "name": "Area B - KwaZulu-Natal",
                "description": (
                    "All Areas in KwaZulu-Natal. The gazette states no figure: conditions of "
                    "employment and minimum wage rates for KwaZulu-Natal areas are subject to the "
                    "collective agreement concluded in the Bargaining Council for the Contract "
                    "Cleaning Service Industry (BCCCI)."
                ),
                "uses_bargaining_council_rates": True,
            },
            {
                "sector": "CONTRACT_CLEANING",
                "code": "AREA_C",
                "name": "Area C - the rest of the RSA",
                "description": (
                    "In the rest of the RSA. This is a RESIDUAL, not a list: anywhere that is "
                    "neither one of the Area A councils nor in KwaZulu-Natal. It is therefore the "
                    "area most contract cleaning workplaces fall into, and it has no "
                    "municipality_area_map rows by design."
                ),
            },
        ],
        # Only the twelve municipalities the determination NAMES. Area B is a province
        # and Area C is a residual, so neither is a list and neither has rows (D-118).
        "municipality_area_map": [
            {
                "sector_area": "AREA_A",
                "province_code": "WC",
                "municipality_name": "City of Cape Town",
                "municipality_type": "metro",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Greater East Rand Metro",
                "municipality_type": "metro",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "City of Johannesburg",
                "municipality_type": "metro",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Tshwane",
                "municipality_type": "metro",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "EC",
                "municipality_name": "Nelson Mandela",
                "municipality_type": "metro",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Emfuleni",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Merafong",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Mogale City",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "FS",
                "municipality_name": "Metsimaholo",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Randfontein",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "WC",
                "municipality_name": "Stellenbosch",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
            {
                "sector_area": "AREA_A",
                "province_code": "GP",
                "municipality_name": "Westonaria",
                "municipality_type": "local",
                "effective_from": "2026-03-01",
            },
        ],
        "minimum_wage_rate": [
            {
                "hourly_rate": "30.2300",
                "effective_from": "2026-03-01",
                "source_reference": NMW_GAZETTE,
                "source_url": NMW_URL,
                "notes": (
                    "The general National Minimum Wage, and the floor beneath every "
                    "sector. The gazette states an hourly figure only."
                ),
            },
            {
                "sector": "DOMESTIC",
                "hourly_rate": "30.2300",
                "effective_from": "2026-03-01",
                "source_reference": NMW_GAZETTE,
                "source_url": NMW_URL,
                "notes": (
                    "Domestic workers are at the general National Minimum Wage, not "
                    "below it. Stored as its own row rather than resolved through the "
                    "NMW fallback so that the sector's figure is explicit and a future "
                    "divergence is a data load."
                ),
            },
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_A",
                "hourly_rate": "33.2700",
                "weekly_rate_45h": "1497.15",
                "monthly_rate_45h": "6487.15",
                "effective_from": "2026-03-01",
                "source_reference": NMW_GAZETTE,
                "source_url": NMW_URL,
                "notes": (
                    "Weekly and monthly figures stored as gazetted, not recomputed. Weekly is on a"
                    " 45-hour week and monthly on 4.333 weeks, per the gazette's own footnotes. "
                    "Applies to the listed metros AND the listed local councils - both sit in this"
                    " one column."
                ),
            },
            {
                "sector": "CONTRACT_CLEANING",
                "sector_area": "AREA_C",
                "hourly_rate": "30.3300",
                "weekly_rate_45h": "1364.85",
                "monthly_rate_45h": "5913.90",
                "effective_from": "2026-03-01",
                "source_reference": NMW_GAZETTE,
                "source_url": NMW_URL,
                "notes": (
                    "Weekly and monthly figures stored as gazetted, not recomputed. THIS IS THE "
                    "RESIDUAL RATE - the rest of the RSA, which is most of the country. Area B "
                    "(KwaZulu-Natal) deliberately has no row: the gazette gives it no figure and "
                    "points at the BCCCI agreement."
                ),
            },
            # Area B (KwaZulu-Natal) is deliberately absent. The gazette gives no figure
            # for it and points at the BCCCI collective agreement, and inventing one would
            # be exactly the failure this phase exists to prevent. It loads when the
            # agreement is in hand, as its own version.
        ],
        "tax_year": [
            {
                "label": "2026/2027",
                "start_date": "2026-03-01",
                "end_date": "2027-02-28",
                "is_open": True,
            }
        ],
        "paye_tax_bracket": brackets(),
        "paye_rebate": [
            {
                "tax_year": "2026/2027",
                "rebate_type": "primary",
                "min_age": 0,
                "annual_amount": "17820.00",
                "tax_threshold_annual": "99000.00",
                "source_reference": SARS_2027,
                "source_url": SARS_URL,
            },
            {
                "tax_year": "2026/2027",
                "rebate_type": "secondary",
                "min_age": 65,
                "annual_amount": "9765.00",
                "tax_threshold_annual": "153250.00",
                "source_reference": SARS_2027,
                "source_url": SARS_URL,
            },
            {
                "tax_year": "2026/2027",
                "rebate_type": "tertiary",
                "min_age": 75,
                "annual_amount": "3249.00",
                "tax_threshold_annual": "171300.00",
                "source_reference": SARS_2027,
                "source_url": SARS_URL,
            },
        ],
        "medical_tax_credit_rate": [
            {
                "tax_year": "2026/2027",
                "main_member_monthly": "376.00",
                "first_dependant_monthly": "376.00",
                "additional_dependant_monthly": "254.00",
                "source_reference": (
                    "SARS Medical Tax Credit Rates, 2027 tax year (1 March 2026 - 28 February 2027)"
                ),
                "source_url": "https://www.sars.gov.za/tax-rates/medical-tax-credit-rates/",
                "notes": (
                    "SARS publishes R376 for the taxpayer and R752 for the taxpayer and "
                    "one dependant. Stored per tier: R376 main member, R376 first "
                    "dependant, R254 each additional."
                ),
            }
        ],
        "statutory_parameter": [
            {
                "parameter_code": "UIF_EMPLOYEE_RATE_PCT",
                "value_numeric": "1.000000",
                "unit": "percent",
                "effective_from": "2002-04-01",
                "source_reference": "Unemployment Insurance Contributions Act 4 of 2002, s6(1)(a)",
                "source_url": UIC_ACT_URL,
            },
            {
                "parameter_code": "UIF_EMPLOYER_RATE_PCT",
                "value_numeric": "1.000000",
                "unit": "percent",
                "effective_from": "2002-04-01",
                # s6(1)(a)(ii), not s6(1)(b): (b) is the percentage the Minister may
                # announce in the budget instead, which is a different rule with a
                # different effective date. Corrected in P7 chunk 1 (D-211) against
                # the consolidated Act.
                "source_reference": (
                    "Unemployment Insurance Contributions Act 4 of 2002, s6(1)(a)(ii)"
                ),
                "source_url": UIC_ACT_URL,
            },
            {
                "parameter_code": "UIF_MONTHLY_CEILING",
                "value_numeric": "17712.000000",
                "unit": "ZAR",
                "effective_from": "2021-06-01",
                # s6(2), not s6(3): the consolidated Act has no s6(3). And the
                # gazette is now named, because "published 28 May 2021" is not a
                # citation anybody can turn to (D-211).
                "source_reference": (
                    "Determination under s6(2) of the Unemployment Insurance "
                    "Contributions Act 4 of 2002, Government Gazette 44641 of "
                    "28 May 2021, effective 1 June 2021"
                ),
                "source_url": UIF_CEILING_URL,
                "notes": (
                    "VERIFY THE GAZETTE NUMBER - not confirmed from a primary source. "
                    "Unchanged since 1 June 2021, which is the point: this ceiling moves "
                    "on ministerial notice with no fixed calendar and is the parameter "
                    "most often missed."
                ),
            },
            {
                "parameter_code": "SDL_RATE_PCT",
                "value_numeric": "1.000000",
                "unit": "percent",
                "effective_from": "2026-03-01",
                "source_reference": "Skills Development Levies Act 9 of 1999, s3(1)",
                "source_url": SDL_ACT_URL,
                "notes": (
                    "effective_from is the start of this reference data rather than the "
                    "levy's commencement, which was not confirmed from a primary source."
                ),
            },
            {
                "parameter_code": "SDL_ANNUAL_EXEMPTION",
                "value_numeric": "500000.000000",
                "unit": "ZAR",
                "effective_from": "2026-03-01",
                "source_reference": "Skills Development Levies Act 9 of 1999, s4(b)",
                "source_url": SDL_ACT_URL,
                "notes": (
                    "FORWARD-LOOKING. The test is whether the employer reasonably "
                    "believes leviable amounts over the NEXT twelve months will exceed "
                    "this figure. It cannot be computed from payroll history, and any "
                    "attempt to derive it from last year's payroll is wrong. "
                    "effective_from is the start of this reference data, not the "
                    "provision's commencement."
                ),
            },
            {
                "parameter_code": "COIDA_ANNUAL_CEILING",
                "value_numeric": "668000.000000",
                "unit": "ZAR",
                "effective_from": "2026-03-01",
                "source_reference": (
                    "Compensation Fund maximum amount of earnings, 2026/2027 assessment "
                    "year (1 March 2026 - 28 February 2027)"
                ),
                "source_url": COIDA_URL,
                "notes": (
                    "The figure came from a payroll vendor's published table rather "
                    "than from the notice, which made it the least well sourced number "
                    "in this load. THE NOTICE IS NOW CITED AND AGREES: Notice 3910 in "
                    "GG 54577, 24 April 2026 prescribes 'the amount of R668 000 per "
                    "employee per annum ... effective from 1st March 2026', with a "
                    "minimum assessment of R1 621 (D-274). The gazette POST-DATES the "
                    "figure it makes effective, which is why its date is deliberately "
                    "not in the citation - see D-274 on what that would do to "
                    "checkstatutory's commencement check."
                ),
            },
            {
                "parameter_code": "BCEA_EARNINGS_THRESHOLD",
                "value_numeric": "261748.450000",
                "unit": "ZAR",
                "effective_from": "2025-04-01",
                "effective_to": "2026-05-01",
                "source_reference": (
                    "GN 5970, GG 52232, March 2025 (Basic Conditions of Employment Act "
                    "75 of 1997, s6(3))"
                ),
                "source_url": THRESHOLD_2025_URL,
                "notes": (
                    "In force for the first two months of this reference version. A high "
                    "earner loses only s18(3) - they keep the public holiday pay "
                    "entitlement."
                ),
            },
            {
                "parameter_code": "BCEA_EARNINGS_THRESHOLD",
                "value_numeric": "269600.900000",
                "unit": "ZAR",
                "effective_from": "2026-05-01",
                "source_reference": (
                    "GN 7384, GG 54544, 17 April 2026 (Basic Conditions of Employment "
                    "Act 75 of 1997, s6(3))"
                ),
                "source_url": THRESHOLD_2026_URL,
            },
            {
                "parameter_code": "VAT_RATE_PCT",
                "value_numeric": "15.000000",
                "unit": "percent",
                "effective_from": "2026-03-01",
                "source_reference": "Value-Added Tax Act 89 of 1991, s7(1)(a)",
                "source_url": VAT_ACT_URL,
                "notes": (
                    "Still 15 percent. The increases announced in the 2025 Budget were "
                    "withdrawn and the 2026 Budget did not reinstate them. "
                    "effective_from is the start of this reference data."
                ),
            },
        ],
        "public_holiday": public_holidays(2026) + public_holidays(2027),
        "statutory_watch_item": [
            {
                "watch_code": "NMW_ANNUAL",
                "name": "National Minimum Wage and sectoral schedules",
                "description": (
                    "The NMW Commission's recommendation is gazetted for comment and then "
                    "finally, effective 1 March. Check the general rate, the EPWP rate and "
                    "the contract cleaning schedule, which moves with it."
                ),
                "change_cadence": "annual_1_march",
                "typical_publication_window": "late January to mid February",
                "next_expected_date": "2027-02-15",
                "last_confirmed_date": "2026-09-13",
                "last_change_effective_date": "2026-03-01",
                "source_name": "Department of Employment and Labour gazette",
                "source_url": NMW_URL,
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "SARS_TAX_TABLES",
                "name": "PAYE brackets, rebates, thresholds and medical credits",
                "description": (
                    "Announced in the Budget and effective 1 March. All four move "
                    "together; a year where the brackets are unchanged still needs the "
                    "medical credits checked."
                ),
                "change_cadence": "annual_1_march",
                "typical_publication_window": "Budget speech, late February",
                "next_expected_date": "2027-02-24",
                "last_confirmed_date": "2026-09-13",
                "last_change_effective_date": "2026-03-01",
                "source_name": "SARS rates of tax for individuals",
                "source_url": SARS_URL,
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "BCEA_EARNINGS_THRESHOLD",
                "name": "BCEA earnings threshold",
                "description": (
                    "Determined under s6(3) and usually effective 1 April or 1 May, on "
                    "its own calendar rather than the tax year's."
                ),
                "change_cadence": "annual_1_may",
                "typical_publication_window": "March to April",
                "next_expected_date": "2027-04-15",
                "last_confirmed_date": "2026-09-13",
                "last_change_effective_date": "2026-05-01",
                "source_name": "Department of Employment and Labour gazette",
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "UIF_MONTHLY_CEILING",
                "name": "UIF contribution ceiling",
                "description": (
                    "Moves on ministerial notice with NO fixed calendar - it sat unchanged "
                    "from October 2012 to June 2021 and has not moved since. The parameter "
                    "most often missed in South African payroll, precisely because there "
                    "is no date to watch."
                ),
                "change_cadence": "irregular",
                "typical_publication_window": "no fixed window - swept quarterly",
                "last_confirmed_date": "2026-09-13",
                "last_change_effective_date": "2021-06-01",
                "source_name": "Department of Employment and Labour gazette",
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "COIDA_ANNUAL_CEILING",
                "name": "COIDA maximum amount of earnings",
                "description": (
                    "Increased by the Minister for each assessment year, usually announced "
                    "shortly before the Return of Earnings season opens."
                ),
                "change_cadence": "irregular",
                "typical_publication_window": "February to April",
                "next_expected_date": "2027-03-31",
                "last_confirmed_date": "2026-09-13",
                "last_change_effective_date": "2026-03-01",
                "source_name": "Compensation Fund notice",
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "PUBLIC_HOLIDAYS",
                "name": "Public holidays, including proclaimed once-off days",
                "description": (
                    "The twelve statutory days are computable, but election days and days "
                    "of mourning are proclaimed with little notice and follow no rule. "
                    "Load the next calendar year each December."
                ),
                "change_cadence": "as_proclaimed",
                "typical_publication_window": "any time, by proclamation",
                "next_expected_date": "2026-12-01",
                "last_confirmed_date": "2026-09-13",
                "source_name": "Public Holidays Act and Presidential proclamations",
                "responsible_role": "Statutory data owner",
            },
            {
                "watch_code": "BCCCI_KZN_AGREEMENT",
                "name": "Contract cleaning KwaZulu-Natal collective agreement",
                "description": (
                    "Area B (KwaZulu-Natal) has no gazetted figure. Its wages come "
                    "from the BCCCI "
                    "collective agreement, which is extended by the Minister and runs on "
                    "its own cycle. NOT YET LOADED - the agreement is needed before any "
                    "KwaZulu-Natal contract cleaning employer can be onboarded."
                ),
                "change_cadence": "irregular",
                "typical_publication_window": "per the council's agreement cycle",
                "status": "change_published_not_loaded",
                "source_name": "Bargaining Council for the Contract Cleaning Service Industry",
                "responsible_role": "Statutory data owner",
            },
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


OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "reference" / "ref-2026.03.01.json"


def main():
    OUTPUT.write_text(json.dumps(DOCUMENT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.name}")


if __name__ == "__main__":
    main()
