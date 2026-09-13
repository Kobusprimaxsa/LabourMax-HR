"""The loader, and the seven refusals that make a statutory load safe.

Each test here corresponds to a way a rate table gets quietly corrupted in a payroll
system. None of them is hypothetical: a half-applied load, a rate edited in place, a
fixture carrying integer ids from another environment, a new rate loaded while the
old one is still open-ended, and a load that marks itself verified are the five that
do real damage, and the two remaining are what makes the first five detectable.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest

from statutory.loader import (
    ReferenceDataLoadError,
    fingerprint,
    load_reference_data,
    load_reference_file,
)
from statutory.models import (
    MinimumWageRate,
    ReferenceDataVersion,
    Sector,
    StatutoryParameter,
)

CITATION = "Test fixture, not a real gazette"


def document(**overrides):
    base = {
        "version_label": "REF-TEST-01",
        "applies_from": "2026-03-01",
        "description": "A test load.",
        "tables": {
            "sector": [
                {
                    "code": "DOMESTIC",
                    "name": "Domestic worker sector",
                    "determination_reference": "Sectoral Determination 7",
                }
            ],
            "minimum_wage_rate": [
                {
                    "sector": "DOMESTIC",
                    "hourly_rate": "20.0000",
                    "effective_from": "2026-03-01",
                    "source_reference": CITATION,
                }
            ],
        },
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------- a good load


@pytest.mark.statutory
def test_a_well_formed_load_creates_the_rows_and_the_version(db):
    report = load_reference_data(document())

    assert report.created["sector"] == 1
    assert report.created["minimum_wage_rate"] == 1
    assert Sector.objects.get(code="DOMESTIC").name == "Domestic worker sector"
    assert MinimumWageRate.objects.count() == 1

    version = ReferenceDataVersion.objects.get(version_label="REF-TEST-01")
    assert version.checksum
    assert version.applies_from == datetime.date(2026, 3, 1)


@pytest.mark.statutory
def test_a_load_is_never_verified_by_itself(db):
    """The load and the check are two acts by two people. This is the load."""
    load_reference_data(document())
    version = ReferenceDataVersion.objects.get(version_label="REF-TEST-01")

    assert version.verified_at is None
    assert version.data_current_through is None
    assert version.is_usable is False
    assert ReferenceDataVersion.in_force_on(datetime.date(2026, 6, 1)) is None


@pytest.mark.statutory
def test_a_fixture_references_a_sector_by_code_not_by_id(db):
    """Integer ids differ between development, CI and production. Codes do not."""
    load_reference_data(document())
    wage = MinimumWageRate.objects.get()
    assert wage.sector.code == "DOMESTIC"


# ------------------------------------------------------------------ the refusals


@pytest.mark.statutory
def test_a_row_without_a_citation_refuses_the_whole_file(db):
    """And the message names the row, because the reader is holding a gazette."""
    bad = document()
    bad["tables"]["minimum_wage_rate"][0]["source_reference"] = "   "

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(bad)

    assert "minimum_wage_rate[1]" in str(caught.value)
    assert Sector.objects.count() == 0, "The sector rows must have rolled back with it."
    assert ReferenceDataVersion.objects.count() == 0


@pytest.mark.statutory
def test_an_unknown_table_refuses_the_file(db):
    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(document(tables={"employee_salary": [{}]}))
    assert "employee_salary" in str(caught.value)


@pytest.mark.statutory
def test_an_unknown_column_refuses_the_file(db):
    bad = document()
    bad["tables"]["minimum_wage_rate"][0]["hourly_rate_zar"] = "20.00"

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(bad)
    assert "hourly_rate_zar" in str(caught.value)


@pytest.mark.statutory
def test_a_reference_to_something_not_loaded_refuses_the_file(db):
    bad = document()
    bad["tables"]["minimum_wage_rate"][0]["sector"] = "HOSPITALITY"

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(bad)
    assert "HOSPITALITY" in str(caught.value)
    assert Sector.objects.count() == 0


@pytest.mark.statutory
def test_a_malformed_date_refuses_the_file(db):
    bad = document()
    bad["tables"]["minimum_wage_rate"][0]["effective_from"] = "01/03/2026"

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(bad)
    assert "YYYY-MM-DD" in str(caught.value)


@pytest.mark.statutory
def test_nothing_is_written_when_a_later_row_fails(db):
    """All or nothing. A half-loaded rate table answers three questions right and the
    fourth one wrong."""
    bad = document()
    bad["tables"]["minimum_wage_rate"].append(
        {
            "sector": "DOMESTIC",
            "hourly_rate": "25.0000",
            "effective_from": "2026-03-01",
            "source_reference": "",
        }
    )

    with pytest.raises(ReferenceDataLoadError):
        load_reference_data(bad)

    assert Sector.objects.count() == 0
    assert MinimumWageRate.objects.count() == 0


# -------------------------------------------------------- never edited in place


@pytest.mark.statutory
def test_reloading_an_identical_fixture_is_a_no_op(db):
    load_reference_data(document())
    second = load_reference_data(document())

    assert second.already_loaded is True
    assert MinimumWageRate.objects.count() == 1


@pytest.mark.statutory
def test_reloading_a_changed_fixture_under_the_same_label_is_refused(db):
    """The fingerprint is what makes a quietly edited fixture detectable."""
    load_reference_data(document())

    edited = document()
    edited["tables"]["minimum_wage_rate"][0]["hourly_rate"] = "21.0000"

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(edited)

    assert "already been loaded from a different fixture" in str(caught.value)
    assert MinimumWageRate.objects.get().hourly_rate == Decimal("20.0000")


@pytest.mark.statutory
def test_a_changed_value_under_a_new_version_is_still_refused(db):
    """The important one. A rate is never edited — it is superseded.

    Same scope, same effective date, different figure, under a brand new version
    label: this is what a correction looks like when someone does it the wrong way,
    and it is exactly what would silently change what a 2026 payslip reproduces.
    """
    load_reference_data(document())

    correction = document(version_label="REF-TEST-02")
    correction["tables"]["minimum_wage_rate"][0]["hourly_rate"] = "21.0000"

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_data(correction)

    assert "never edited" in str(caught.value)
    assert "hourly_rate" in str(caught.value)


@pytest.mark.statutory
def test_an_unchanged_row_in_a_later_version_is_counted_not_refused(db):
    """A load that repeats last year's unchanged sector rows must not be a fight."""
    load_reference_data(document())
    report = load_reference_data(document(version_label="REF-TEST-02"))

    assert report.unchanged["sector"] == 1
    assert report.unchanged["minimum_wage_rate"] == 1
    assert report.total_created == 0


# ------------------------------------------------------- superseding a period


@pytest.mark.statutory
def test_a_new_rate_collides_with_an_open_period_unless_closing_is_declared(db):
    """The realistic annual load: last year's row is still open-ended.

    Without a declaration the exclusion constraint refuses it, which is correct —
    the loader does not quietly modify verified data.
    """
    load_reference_data(document())

    next_year = document(version_label="REF-TEST-02")
    next_year["tables"]["minimum_wage_rate"][0]["effective_from"] = "2027-03-01"
    next_year["tables"]["minimum_wage_rate"][0]["hourly_rate"] = "22.0000"

    from django.db.utils import IntegrityError

    with pytest.raises(IntegrityError):
        load_reference_data(next_year)


@pytest.mark.statutory
def test_declaring_the_close_date_ends_the_previous_period_and_reports_it(db):
    load_reference_data(document())

    next_year = document(version_label="REF-TEST-02", closes_open_periods_from="2027-03-01")
    next_year["tables"]["minimum_wage_rate"][0]["effective_from"] = "2027-03-01"
    next_year["tables"]["minimum_wage_rate"][0]["hourly_rate"] = "22.0000"

    report = load_reference_data(next_year)

    assert report.closed_periods, "Every closed period must be reported for verification."
    previous = MinimumWageRate.objects.get(effective_from=datetime.date(2026, 3, 1))
    assert previous.effective_to == datetime.date(2027, 3, 1)
    assert MinimumWageRate.objects.count() == 2


@pytest.mark.statutory
def test_closing_never_touches_a_different_scope(db):
    """Contract cleaning's open period must survive a domestic-only load."""
    first = document()
    first["tables"]["sector"].append(
        {
            "code": "CONTRACT_CLEANING",
            "name": "Contract cleaning sector",
            "uses_area_rates": True,
        }
    )
    first["tables"]["minimum_wage_rate"].append(
        {
            "sector": "CONTRACT_CLEANING",
            "hourly_rate": "30.0000",
            "effective_from": "2026-03-01",
            "source_reference": CITATION,
        }
    )
    load_reference_data(first)

    domestic_only = {
        "version_label": "REF-TEST-02",
        "applies_from": "2027-03-01",
        "closes_open_periods_from": "2027-03-01",
        "tables": {
            "minimum_wage_rate": [
                {
                    "sector": "DOMESTIC",
                    "hourly_rate": "22.0000",
                    "effective_from": "2027-03-01",
                    "source_reference": CITATION,
                }
            ]
        },
    }
    load_reference_data(domestic_only)

    cleaning_rate = MinimumWageRate.objects.get(sector__code="CONTRACT_CLEANING")
    assert cleaning_rate.effective_to is None


@pytest.mark.statutory
def test_a_parameter_period_closes_on_its_own_code_only(db):
    """The scope for a scalar parameter is its code, not the whole table."""
    load_reference_data(
        {
            "version_label": "REF-TEST-PARAMS",
            "applies_from": "2026-03-01",
            "tables": {
                "statutory_parameter": [
                    {
                        "parameter_code": "TEST_A",
                        "value_numeric": "1.000000",
                        "effective_from": "2026-03-01",
                        "source_reference": CITATION,
                    },
                    {
                        "parameter_code": "TEST_B",
                        "value_numeric": "2.000000",
                        "effective_from": "2026-03-01",
                        "source_reference": CITATION,
                    },
                ]
            },
        }
    )

    load_reference_data(
        {
            "version_label": "REF-TEST-PARAMS-2",
            "applies_from": "2027-03-01",
            "closes_open_periods_from": "2027-03-01",
            "tables": {
                "statutory_parameter": [
                    {
                        "parameter_code": "TEST_A",
                        "value_numeric": "3.000000",
                        "effective_from": "2027-03-01",
                        "source_reference": CITATION,
                    }
                ]
            },
        }
    )

    assert StatutoryParameter.objects.get(
        parameter_code="TEST_A", effective_from=datetime.date(2026, 3, 1)
    ).effective_to == datetime.date(2027, 3, 1)
    assert StatutoryParameter.objects.get(parameter_code="TEST_B").effective_to is None, (
        "A parameter nobody reloaded must keep its open period."
    )


# --------------------------------------------------------------- the fingerprint


@pytest.mark.statutory
def test_the_fingerprint_ignores_key_order():
    """Otherwise a re-serialised but identical fixture reads as a changed one."""
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


@pytest.mark.statutory
def test_the_fingerprint_notices_a_changed_figure():
    assert fingerprint({"rate": "20.0000"}) != fingerprint({"rate": "20.0001"})


@pytest.mark.statutory
def test_a_file_on_disk_loads_the_same_way(db, tmp_path):
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(document()), encoding="utf-8")

    report = load_reference_file(path)
    assert report.created["minimum_wage_rate"] == 1


@pytest.mark.statutory
def test_a_file_that_is_not_json_says_so(db, tmp_path):
    path = tmp_path / "reference.json"
    path.write_text("this is a spreadsheet, not a fixture", encoding="utf-8")

    with pytest.raises(ReferenceDataLoadError) as caught:
        load_reference_file(path)
    assert "not valid JSON" in str(caught.value)
