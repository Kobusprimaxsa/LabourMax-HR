"""Year-to-date totals: a cache, and the one thing that rebuilds it.

Invariant 3: "Year-to-date figures come from finalised payslips. Both caches can
be discarded and rebuilt from source rows. Never 'fix' a balance by writing to
it."

**One row per employee, tax year and employer — sheet 02's shape** (D-290,
superseding D-231's row per source code). The named totals are what the
payslip's YTD column and the EMP201 read; ``ytd_by_source_code`` is what the
IRP5 reads, one key per SARS code, so the three components that share 3601 land
on one figure there.

**There is no incremental update path, and that is the design** — P5 settled the
same question for ``timesheet_summary`` (D-153), and D-231's point survives the
reshape: a cache maintained incrementally that has drifted cannot be told apart
from a correct one. ``rebuild()`` recomputes the whole employee-year from the
finalised payslips every time, and ``disagreements_with_source()`` is the
ground truth that never looks at the cache's own bookkeeping.

**Only FINALISED payslips count.** A draft is a working figure that will change
before anyone is paid. A REVERSING payslip does count: its figures are negative
and netting them is precisely how a correction reaches the IRP5.

**The COIDA figure is capped by ``payroll/coida.py``, not here** (D-285): the
cap is annual, per employee, applied once, and there is one place that applies
it. The assessment period's dates coincide with the tax year's, so the tax year
is handed in as the period.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.managers import tenant_context_of
from payroll import coida
from payroll.models import Payslip, PayslipLine, YtdAccumulator

ZERO = Decimal("0")

#: Named total on the cache  ->  the payslip column it sums.
NAMED_TOTALS = {
    "ytd_gross": "gross_remuneration",
    "ytd_taxable": "taxable_remuneration",
    "ytd_paye": "paye",
    "ytd_uif_employee": "uif_employee",
    "ytd_uif_employer": "uif_employer",
    "ytd_sdl": "sdl_employer",
    "ytd_uif_remuneration": "uif_remuneration",
}


@dataclasses.dataclass(frozen=True)
class Disagreement:
    """One figure where the cache and the finalised payslips differ. ``figure``
    is a named total (``ytd_paye``) or a SARS source code (``3601``)."""

    figure: str
    cached_amount: Decimal
    actual_amount: Decimal

    def __str__(self) -> str:
        return (
            f"{self.figure}: cache says {self.cached_amount}, the finalised "
            f"payslips say {self.actual_amount}"
        )


def _finalised(employee, tax_year):
    return Payslip.objects.filter(
        employee=employee,
        is_finalised=True,
        payroll_run__pay_period__tax_year=tax_year,
    )


def _periods_processed(payslips) -> int:
    """Pay periods with a finalised payslip that no reversal has undone. A period
    reversed and then paid again counts once; a period reversed and left
    unpaid counts not at all."""
    reversed_ids = {p.reverses_payslip_id for p in payslips if p.is_reversal}
    return len(
        {p.pay_period_id for p in payslips if not p.is_reversal and p.pk not in reversed_ids}
    )


def _truth(employee, tax_year) -> dict:
    """Every figure the cache holds, computed from the finalised payslips."""
    payslips = list(_finalised(employee, tax_year))
    figures = {
        name: _finalised(employee, tax_year).aggregate(total=Sum(column))["total"] or ZERO
        for name, column in NAMED_TOTALS.items()
    }
    by_code = (
        PayslipLine.objects.filter(payslip__in=[p.pk for p in payslips])
        .exclude(source_code="")
        .values("source_code")
        .annotate(amount=Sum("amount"))
        .order_by("source_code")
    )
    figures["ytd_by_source_code"] = {row["source_code"]: row["amount"] for row in by_code}
    figures["ytd_coida_remuneration"] = (
        coida.employee_earnings(
            employee,
            period_start=tax_year.start_date,
            period_end=tax_year.end_date,
            calculated_for=tax_year.end_date,
        ).declared.rounded
        if payslips
        else ZERO
    )
    figures["periods_processed"] = _periods_processed(payslips)
    last = max(payslips, key=lambda p: (p.finalised_at, p.pk), default=None)
    figures["last_payroll_run_id"] = None if last is None else last.payroll_run_id
    return figures


@transaction.atomic
def rebuild(employee, tax_year, *, as_at: datetime.datetime | None = None) -> list[YtdAccumulator]:
    """Discard this employee-year's cache and recompute it from the payslips.

    Deletes first, so an employee whose only payslip was reversed out of the
    year leaves no stale row behind. Returns the rebuilt row in a list — empty
    when nothing is finalised — so callers can count what was written.

    ``as_at`` stamps the row; it is never read as "when to compute up to",
    because the tax year already says that.
    """
    stamped = as_at or timezone.now()

    with tenant_context_of(employee):
        truth = _truth(employee, tax_year)
        YtdAccumulator.objects.filter(employee=employee, tax_year=tax_year).delete()
        if not _finalised(employee, tax_year).exists():
            return []
        return [
            YtdAccumulator.objects.create(
                tenant=employee.tenant,
                employee=employee,
                tax_year=tax_year,
                employer=employee.employer,
                periods_processed=truth["periods_processed"],
                ytd_coida_remuneration=truth["ytd_coida_remuneration"],
                ytd_by_source_code={
                    code: str(amount) for code, amount in truth["ytd_by_source_code"].items()
                },
                last_payroll_run_id=truth["last_payroll_run_id"],
                recalculated_at=stamped,
                **{name: truth[name] for name in NAMED_TOTALS},
            )
        ]


def disagreements_with_source(employee, tax_year) -> list[Disagreement]:
    """Every figure where the cache and the finalised payslips disagree.

    The ground truth, and it does not consult ``recalculated_at`` or any other
    flag the cache keeps about itself (D-153). A cache that was never rebuilt,
    or was edited by hand, is still caught, because this compares figures.
    """
    with tenant_context_of(employee):
        truth = _truth(employee, tax_year)
        row = YtdAccumulator.objects.filter(employee=employee, tax_year=tax_year).first()

    found = []
    for name in (*NAMED_TOTALS, "ytd_coida_remuneration"):
        held = getattr(row, name) if row is not None else ZERO
        if held != truth[name]:
            found.append(Disagreement(figure=name, cached_amount=held, actual_amount=truth[name]))

    cached_codes = {
        code: Decimal(amount) for code, amount in (row.ytd_by_source_code if row else {}).items()
    }
    actual_codes = truth["ytd_by_source_code"]
    for code in sorted(set(cached_codes) | set(actual_codes)):
        held = cached_codes.get(code, ZERO)
        actual = actual_codes.get(code, ZERO)
        if held != actual:
            found.append(Disagreement(figure=code, cached_amount=held, actual_amount=actual))
    return found


def total_for(employee, tax_year, *, source_codes: list[str] | None = None) -> Decimal:
    """The year-to-date total across source codes, read from the CACHE.

    Reads the cache deliberately: this is the fast path a payslip screen and the
    PAYE annual-equivalent both want. A caller that needs certainty runs
    ``disagreements_with_source()`` first.
    """
    with tenant_context_of(employee):
        row = YtdAccumulator.objects.filter(employee=employee, tax_year=tax_year).first()
    if row is None:
        return ZERO
    return sum(
        (
            Decimal(amount)
            for code, amount in row.ytd_by_source_code.items()
            if source_codes is None or code in source_codes
        ),
        ZERO,
    )


def finalised_payslips(employee, tax_year):
    """The rows every figure above is derived from, for anyone checking one."""
    with tenant_context_of(employee):
        return list(
            _finalised(employee, tax_year).order_by("payroll_run__pay_period__payment_date")
        )
