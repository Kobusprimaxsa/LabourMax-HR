"""Consistency checks over loaded reference data — arithmetic the source can settle.

A transcription error in a rate table is invisible. Nothing errors, the payslip
looks plausible, and the figure is wrong by one digit in one band. Proofreading
catches some of it; arithmetic catches more, because published statutory tables are
internally redundant in ways a typo breaks.

The PAYE table is the clearest example. SARS prints each band as "R44 118 + 26% of
taxable income above R245 100", and that R44 118 is *derivable* from the bands below
it: 18% of 245 100. Every base in the table is the running total of the bands beneath
it, so one mistyped digit anywhere makes the whole ladder stop reconciling. The same
holds for the rebates: the tax threshold for each age is exactly the income at which
the cumulative rebates cancel the tax, so 99 000 x 18% must equal the primary rebate
of 17 820 to the cent.

Neither check knows what the correct figures *are*. They only know the figures must
agree with each other, which is enough to catch the error a human proofreader misses
and cheap enough to run on every verification.

These are also the hook for P7's golden tests: this module fails a version on
arithmetic, and the golden tests will fail it on published worked examples.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from statutory.models import MinimumWageRate, PayeRebate, TaxYear

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Issue:
    """One thing that does not add up. ``blocking`` decides whether it stops a load."""

    blocking: bool
    where: str
    message: str

    def __str__(self):
        marker = "REFUSED" if self.blocking else "warning"
        return f"[{marker}] {self.where}: {self.message}"


def check_paye_brackets(year: TaxYear) -> list[Issue]:
    """Every band's cumulative base must equal the tax on all the bands below it.

    Also checks that the bands are contiguous and that the top one is open-ended.
    A gap between bands is worse than an overlap: an income that falls in the gap
    resolves to no band at all, and the calculator raises in the middle of a payroll
    run rather than at load time.
    """
    issues = []
    brackets = list(year.brackets.order_by("bracket_order"))

    if not brackets:
        return [Issue(True, year.label, "no PAYE brackets are loaded")]

    if brackets[0].income_from != 0:
        issues.append(
            Issue(
                True,
                f"{year.label} band 1",
                f"starts at {brackets[0].income_from}, not 0. Taxable income below that "
                f"would resolve to no band at all.",
            )
        )
    if brackets[0].base_tax != 0:
        issues.append(
            Issue(True, f"{year.label} band 1", f"carries a base of {brackets[0].base_tax}, not 0")
        )
    if brackets[-1].income_to is not None:
        issues.append(
            Issue(
                True,
                f"{year.label} band {brackets[-1].bracket_order}",
                "is the top band but has an upper bound. The top band must be open-ended.",
            )
        )

    for previous, current in zip(brackets, brackets[1:], strict=False):
        where = f"{year.label} band {current.bracket_order}"

        if previous.income_to is None:
            issues.append(
                Issue(True, where, f"band {previous.bracket_order} below it is open-ended")
            )
            continue

        if current.income_from != previous.income_to:
            issues.append(
                Issue(
                    True,
                    where,
                    f"starts at {current.income_from} but the band below ends at "
                    f"{previous.income_to}. Bands must touch exactly.",
                )
            )
            continue

        width = previous.income_to - previous.income_from
        expected = previous.base_tax + (width * previous.marginal_rate_pct / HUNDRED)
        if current.base_tax != expected:
            issues.append(
                Issue(
                    True,
                    where,
                    f"base is {current.base_tax}, but the bands below it come to "
                    f"{expected}. One of the two figures is mistyped.",
                )
            )

    return issues


def check_paye_rebates(year: TaxYear) -> list[Issue]:
    """A tax threshold is the income at which the cumulative rebates cancel the tax.

    SARS publishes both, and they are two views of one figure: the threshold times
    the lowest marginal rate equals the rebates the taxpayer qualifies for. If the
    two disagree, one of them was typed wrong.
    """
    issues = []
    rebates = list(year.rebates.order_by("min_age"))
    if not rebates:
        return [Issue(True, year.label, "no PAYE rebates are loaded")]

    first_band = year.brackets.order_by("bracket_order").first()
    if first_band is None:
        return [Issue(True, year.label, "rebates cannot be checked without the brackets")]

    lowest_rate = first_band.marginal_rate_pct / HUNDRED
    cumulative = Decimal(0)

    for rebate in rebates:
        cumulative += rebate.annual_amount
        expected_threshold = cumulative / lowest_rate
        difference = abs(expected_threshold - rebate.tax_threshold_annual)
        # Published thresholds are whole rand, so a rounding difference under R1 is
        # SARS rounding rather than a transcription error.
        if difference >= 1:
            issues.append(
                Issue(
                    True,
                    f"{year.label} {rebate.rebate_type} rebate",
                    f"cumulative rebates of {cumulative} at {first_band.marginal_rate_pct}% "
                    f"imply a threshold of {expected_threshold:.2f}, but "
                    f"{rebate.tax_threshold_annual} is loaded.",
                )
            )

    return issues


def check_gazetted_wage_derivations(*, hours_per_week: int = 45) -> list[Issue]:
    """Where a gazette prints both an hourly and a weekly rate, they should agree.

    A **warning**, never blocking. Gazettes round, and where the published figures
    disagree with the arithmetic the published figure is what the employer will be
    shown — that is a settled decision, not a defect. What this catches is the larger
    discrepancy, the kind that means a rate was copied into the wrong row.
    """
    issues = []
    for rate in MinimumWageRate.objects.exclude(weekly_rate_45h=None):
        implied = rate.hourly_rate * hours_per_week
        difference = abs(implied - rate.weekly_rate_45h)
        if difference > 1:
            issues.append(
                Issue(
                    False,
                    str(rate),
                    f"{rate.hourly_rate} an hour over {hours_per_week} hours is {implied}, "
                    f"but the weekly rate loaded is {rate.weekly_rate_45h}. Gazettes round, "
                    f"so check this is rounding and not a misplaced row.",
                )
            )
    return issues


def check_citations() -> list[Issue]:
    """Nothing cited to a placeholder.

    The CHECK constraints refuse a blank citation. They cannot refuse a citation
    that says "TBC", and a row that cites nothing real is the one that survives
    verification because it looks filled in.
    """
    placeholders = ("tbc", "tbd", "todo", "unknown", "n/a", "test fixture", "placeholder")
    issues = []
    for model in (MinimumWageRate, PayeRebate):
        for row in model.objects.all():
            reference = (row.source_reference or "").strip().lower()
            if any(reference.startswith(bad) or reference == bad for bad in placeholders):
                issues.append(
                    Issue(True, str(row), f"is cited to a placeholder: {row.source_reference!r}")
                )
    return issues


def run_all(year: TaxYear | None = None) -> list[Issue]:
    """Every check, over every tax year unless one is named."""
    issues = []
    years = [year] if year is not None else list(TaxYear.objects.all())
    for one in years:
        issues.extend(check_paye_brackets(one))
        issues.extend(check_paye_rebates(one))
    issues.extend(check_gazetted_wage_derivations())
    issues.extend(check_citations())
    return issues
