"""The golden tests' figures ARE the loaded version's figures.

A golden test in ``calculators/tests/`` is a pure function called with literal
figures copied from a SARS guide, because a calculator takes its figures as
inputs. That proves the ARITHMETIC against a published answer. On its own it
proves nothing about ``reference/ref-2026.03.01.json`` — the golden file could
say 17 820 while the fixture said 17 280, every golden test would stay green, and
``--golden-tests-passed`` would be recorded against a version that was never
tested. That is the shape of a guard that reads convincingly and does nothing.

So this file joins the two: every figure a golden test hands the PAYE and UIF
calculators for the 2027 tax year is read back out of the shipped fixture and
compared. It is marked ``golden`` because it is part of what the flag asserts:
``pytest -m golden`` passing means the published examples reproduce AND they
reproduce on the figures this version loads. It reads the file and never loads
it — the loaded database is held to the file by ``check_fixture_checksums()``
(D-161), so file and database cannot differ without ``checkstatutory`` saying so.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from calculators.tests.test_paye_golden import (
    BRACKETS_2027,
    CREDIT_2027,
    PRIMARY_2027,
    SECONDARY_2027,
    TERTIARY_2027,
)
from calculators.tests.test_sdl import RATE as SDL_RATE
from calculators.tests.test_uif import CEILING, EMPLOYER_RATE
from calculators.tests.test_uif import RATE as UIF_RATE

pytestmark = pytest.mark.golden

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "reference" / "ref-2026.03.01.json"
TAX_YEAR = "2026/2027"


@pytest.fixture(scope="module")
def tables():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["tables"]


def parameter(tables, code) -> Decimal:
    rows = [row for row in tables["statutory_parameter"] if row["parameter_code"] == code]
    assert len(rows) == 1, f"{code}: expected one row in {FIXTURE.name}, found {len(rows)}"
    return Decimal(rows[0]["value_numeric"])


def test_every_paye_band_the_golden_tests_use_is_the_band_the_fixture_loads(tables):
    loaded = sorted(
        (row for row in tables["paye_tax_bracket"] if row["tax_year"] == TAX_YEAR),
        key=lambda row: row["bracket_order"],
    )
    assert len(loaded) == len(BRACKETS_2027)

    for golden, row in zip(BRACKETS_2027, loaded, strict=True):
        assert golden.income_from == Decimal(row["income_from"]), row
        income_to = row["income_to"]
        assert golden.income_to == (None if income_to is None else Decimal(income_to)), row
        assert golden.base_tax == Decimal(row["base_tax"]), row
        assert golden.marginal_rate_percent == Decimal(row["marginal_rate_pct"]), row


@pytest.mark.parametrize(
    ("rebate_type", "golden", "threshold"),
    [
        ("primary", PRIMARY_2027, Decimal("99000")),
        ("secondary", SECONDARY_2027, Decimal("153250")),
        ("tertiary", TERTIARY_2027, Decimal("171300")),
    ],
)
def test_every_rebate_and_threshold_is_the_one_the_fixture_loads(
    tables, rebate_type, golden, threshold
):
    """The thresholds are the ones ``test_paye_golden.py`` proves produce nil
    tax — G01 §4's R99 000 / R153 250 / R171 300."""
    (row,) = [
        row
        for row in tables["paye_rebate"]
        if row["tax_year"] == TAX_YEAR and row["rebate_type"] == rebate_type
    ]
    assert golden.value == Decimal(row["annual_amount"])
    assert threshold == Decimal(row["tax_threshold_annual"])


def test_the_medical_credit_is_the_one_the_fixture_loads(tables):
    (row,) = [row for row in tables["medical_tax_credit_rate"] if row["tax_year"] == TAX_YEAR]
    assert CREDIT_2027.main_member_monthly == Decimal(row["main_member_monthly"])
    assert CREDIT_2027.first_dependant_monthly == Decimal(row["first_dependant_monthly"])
    assert CREDIT_2027.additional_dependant_monthly == Decimal(row["additional_dependant_monthly"])


def test_the_uif_figures_behind_r177_12_are_the_ones_the_fixture_loads(tables):
    assert parameter(tables, "UIF_MONTHLY_CEILING") == CEILING.value
    assert parameter(tables, "UIF_EMPLOYEE_RATE_PCT") == UIF_RATE.value
    assert parameter(tables, "UIF_EMPLOYER_RATE_PCT") == EMPLOYER_RATE.value


def test_the_sdl_rate_is_the_one_the_fixture_loads(tables):
    assert parameter(tables, "SDL_RATE_PCT") == SDL_RATE.value


def test_this_guard_fails_when_the_fixture_and_the_golden_file_disagree(tables):
    """PROVE EVERY GUARD FAILS: a fixture with one base mistyped — R44 181 for
    R44 118, the transposition a verifier is most likely to miss — is refused
    by the same comparison, naming the row."""
    tampered = json.loads(json.dumps(tables))
    for row in tampered["paye_tax_bracket"]:
        if row["tax_year"] == TAX_YEAR and row["bracket_order"] == 2:
            row["base_tax"] = "44181.00"

    with pytest.raises(AssertionError, match="'bracket_order': 2"):
        test_every_paye_band_the_golden_tests_use_is_the_band_the_fixture_loads(tampered)
