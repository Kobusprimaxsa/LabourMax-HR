"""Resolving a workplace's wage area from where it is.

Contract cleaning pays different minimums in different parts of the country, and the
gazette defines those areas by naming municipalities. So the area is derived from the
workplace's municipality, never asked.

**Why not just ask.** "Are you in Area A or Area C?" gets a confident answer, and a
wrong one is invisible — it produces payslips that look right and underpay by a
couple of rand an hour for as long as nobody checks. Deriving it produces a wrong
answer *visibly*: either the municipality is in the mapping and the area is right, or
it is not and the resolution comes back unresolved, which stops onboarding rather
than guessing.

**The resolved area is stored on the workplace, not computed on read.** Municipal
boundaries and names change — amalgamations have moved workplaces between areas — so
``municipality_area_map`` is effective-dated, and a payroll re-run for 2026 in 2030
must use the area that applied in 2026. The stored value is that answer; this module
is only how it is first obtained.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from statutory.models import MunicipalityAreaMap, Sector
from statutory.resolve import in_force_on


@dataclass(frozen=True)
class AreaResolution:
    """What resolution found, including the two ways it can legitimately find nothing."""

    area = None
    reason: str = ""

    def __init__(self, area=None, reason: str = ""):
        object.__setattr__(self, "area", area)
        object.__setattr__(self, "reason", reason)

    @property
    def resolved(self) -> bool:
        return self.area is not None

    def __bool__(self) -> bool:
        return self.resolved


NOT_AN_AREA_SECTOR = (
    "This sector does not use area rates, so there is no area to resolve. Domestic "
    "employers are at the National Minimum Wage wherever they are."
)

NO_MUNICIPALITY = (
    "The workplace has no municipality recorded, so its area cannot be derived. Ask "
    "for the municipality rather than for the area - an employer asked which area "
    "they are in will answer confidently and may be wrong."
)

NO_MAPPING = (
    "No municipality is mapped to a wage area for this date. The mapping table is "
    "empty or does not cover this municipality, so the area is unknown - which blocks "
    "onboarding rather than defaulting to an area that would underpay or overpay every "
    "employee at this site."
)


def resolve_area(workplace, on_date: datetime.date | None = None) -> AreaResolution:
    """Find the wage area for a workplace, or say why it could not be found.

    Returns a result object rather than raising, because "unresolved" is an ordinary
    state during onboarding — the employer is mid-way through typing an address — and
    an exception would make the normal path an error path. What must never happen is
    a *default*: every non-resolution here is a reason string a screen can show.
    """
    on_date = on_date or datetime.date.today()
    sector = workplace.employer.sector

    if not sector.uses_area_rates:
        return AreaResolution(reason=NOT_AN_AREA_SECTOR)

    if not workplace.municipality.strip():
        return AreaResolution(reason=NO_MUNICIPALITY)

    mapping = (
        in_force_on(
            MunicipalityAreaMap.objects.filter(municipality_name__iexact=workplace.municipality),
            on_date,
        )
        .select_related("sector_area")
        .first()
    )
    if mapping is None:
        return AreaResolution(reason=NO_MAPPING)

    return AreaResolution(area=mapping.sector_area)


def apply_area(workplace, on_date: datetime.date | None = None) -> AreaResolution:
    """Resolve and store, so a re-run reproduces this answer rather than today's.

    Writes both the area and the date it was read on. The pair is enforced by a CHECK
    — an area with no resolution date is an area nobody can audit, and a date with no
    area is a resolution that did not happen.
    """
    on_date = on_date or datetime.date.today()
    resolution = resolve_area(workplace, on_date)

    if resolution.resolved:
        workplace.sector_area = resolution.area
        workplace.area_resolved_on = on_date
    else:
        workplace.sector_area = None
        workplace.area_resolved_on = None

    workplace.save(update_fields=["sector_area", "area_resolved_on", "updated_at"])
    return resolution


def area_rate_sectors():
    """The sectors whose workplaces need an area at all."""
    return Sector.objects.filter(uses_area_rates=True, is_active=True)
