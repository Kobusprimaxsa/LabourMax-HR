"""Year-to-date totals: a cache, and the one thing that rebuilds it.

Invariant 3: "Year-to-date figures come from finalised payslips. Both caches can
be discarded and rebuilt from source rows. Never 'fix' a balance by writing to
it."

**There is no incremental update path, and that is the design** — P5 settled the
same question for ``timesheet_summary`` (D-153): a cache maintained
incrementally that has drifted cannot be told apart from a correct one, so the
only way to maintain it is to rebuild it from scratch and the only way to check
it is to rebuild it and compare. ``rebuild()`` therefore always recomputes the
whole employee-year from the finalised payslip lines, and
``disagreements_with_source()`` is the ground truth that never looks at the
cache's own bookkeeping.

**Only FINALISED payslips count.** A draft payslip is a working figure that will
change before anyone is paid, and a year-to-date total built from drafts moves
under the employer's feet. A REVERSING payslip does count: its lines are
negative and netting them is precisely how a correction reaches the IRP5.

**Keyed on the SARS source code**, because what a year-to-date figure is FOR is
the IRP5 and the EMP201, both of which are stated in source codes. Three
components share 3601 and belong on one line there.
"""

from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from core.managers import tenant_context_of
from payroll.models import Payslip, PayslipLine, YtdAccumulator


@dataclasses.dataclass(frozen=True)
class Disagreement:
    """One source code where the cache and the finalised payslips differ."""

    source_code: str
    cached_amount: Decimal
    actual_amount: Decimal

    def __str__(self) -> str:
        return (
            f"{self.source_code}: cache says {self.cached_amount}, the finalised "
            f"payslips say {self.actual_amount}"
        )


def _totals_from_payslips(employee, tax_year) -> dict[str, dict]:
    """The truth: every finalised payslip line for this employee and tax year.

    A payslip belongs to a run, which belongs to a period, which belongs to the
    tax year its PAYMENT DATE falls in (D-83). That chain is walked here rather
    than denormalised onto the payslip, so there is one answer to "which year is
    this in" and it is the period's.
    """
    rows = (
        PayslipLine.objects.filter(
            payslip__employee=employee,
            payslip__is_finalised=True,
            payslip__payroll_run__pay_period__tax_year=tax_year,
        )
        .values("source_code")
        .annotate(
            amount=Sum("amount"),
            units=Sum("units"),
            payslips=Count("payslip", distinct=True),
        )
    )
    return {
        row["source_code"]: {
            "amount": row["amount"] or Decimal("0"),
            "units": row["units"] or Decimal("0"),
            "payslip_count": row["payslips"],
        }
        for row in rows
    }


@transaction.atomic
def rebuild(employee, tax_year, *, as_at: datetime.datetime | None = None) -> list[YtdAccumulator]:
    """Discard this employee-year's cache and recompute it from the payslips.

    Deletes first, so a source code that no longer has any finalised line — a
    run reversed in full, say — leaves no stale row behind. Rebuilding by
    updating in place is how a cache comes to carry a figure nothing in the
    source can produce.

    ``as_at`` exists so a caller can stamp a batch consistently; it is never
    read as "when to compute up to", because the tax year already says that. A
    service function may not call the clock for its own business logic, but
    stamping a row with the time it was written is bookkeeping, not logic.
    """
    stamped = as_at or timezone.now()

    with tenant_context_of(employee):
        totals = _totals_from_payslips(employee, tax_year)
        YtdAccumulator.objects.filter(employee=employee, tax_year=tax_year).delete()
        return [
            YtdAccumulator.objects.create(
                tenant=employee.tenant,
                employee=employee,
                tax_year=tax_year,
                source_code=source_code,
                amount=figures["amount"],
                units=figures["units"],
                payslip_count=figures["payslip_count"],
                rebuilt_at=stamped,
            )
            for source_code, figures in sorted(totals.items())
        ]


def disagreements_with_source(employee, tax_year) -> list[Disagreement]:
    """Every source code where the cache and the finalised payslips disagree.

    The ground truth, and it does not consult ``rebuilt_at`` or any other flag
    the cache keeps about itself — the same shape as
    ``attendance/summary.py::is_actually_stale()`` (D-153). A cache that was
    never rebuilt, or was edited by hand, is still caught, because this compares
    figures rather than bookkeeping.
    """
    with tenant_context_of(employee):
        actual = _totals_from_payslips(employee, tax_year)
        cached = {
            row.source_code: row
            for row in YtdAccumulator.objects.filter(employee=employee, tax_year=tax_year)
        }

    found = []
    for source_code in sorted(set(actual) | set(cached)):
        truth = actual.get(source_code, {}).get("amount", Decimal("0"))
        row = cached.get(source_code)
        held = row.amount if row is not None else Decimal("0")
        if held != truth:
            found.append(
                Disagreement(source_code=source_code, cached_amount=held, actual_amount=truth)
            )
    return found


def total_for(employee, tax_year, *, source_codes: list[str] | None = None) -> Decimal:
    """The year-to-date total, read from the CACHE.

    Reads the cache deliberately: this is the fast path a payslip screen and the
    PAYE annual-equivalent both want, and the cache is what it is for. A caller
    that needs certainty runs ``disagreements_with_source()`` first — which is
    the only honest way to have both, since checking on every read would make
    the cache pointless.
    """
    with tenant_context_of(employee):
        rows = YtdAccumulator.objects.filter(employee=employee, tax_year=tax_year)
        if source_codes is not None:
            rows = rows.filter(source_code__in=source_codes)
        return rows.aggregate(total=Sum("amount"))["total"] or Decimal("0")


def finalised_payslips(employee, tax_year):
    """The rows every figure above is derived from, for anyone checking one."""
    with tenant_context_of(employee):
        return list(
            Payslip.objects.filter(
                employee=employee,
                is_finalised=True,
                payroll_run__pay_period__tax_year=tax_year,
            ).order_by("payroll_run__pay_period__payment_date")
        )
