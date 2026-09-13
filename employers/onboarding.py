"""What an employer's settings are, and what each sector starts them at.

The registry is here rather than in the database because a setting's *existence*,
type and meaning are code — they are referenced by name in calculators and screens,
and a setting nobody reads is dead weight. Only the *value* is data.

**The line this module has to hold: a setting is not a statutory figure.** The
overtime multiplier is not an employer preference; it is BCEA s10(2) and it lives in
``working_time_rule_set`` with a citation. The one genuinely employer-set figure in
either sector is the night work allowance, because BCEA s17(2) requires an allowance
and deliberately names no amount — and even there, contract cleaning has a gazetted
10 percent (SD1 clause 16), so the setting is the fallback for the sector that has
none rather than the first place to look.

``seed_settings_for`` runs once at onboarding. It records ``set_by_employer=False``,
which is what later distinguishes "this is still the default we gave them" from
"they chose this" — the difference that decides whether changing a sector default
should reach an existing employer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from employers.models import EmployerSetting

DOMESTIC = "DOMESTIC"
CONTRACT_CLEANING = "CONTRACT_CLEANING"


@dataclass(frozen=True)
class SettingDefinition:
    """One setting: what it is, what type it holds, and its default per sector."""

    key: str
    value_type: str
    description: str
    default: object
    by_sector: dict[str, object] | None = None

    def default_for(self, sector_code: str):
        if self.by_sector and sector_code in self.by_sector:
            return self.by_sector[sector_code]
        return self.default


SETTING_DEFINITIONS: list[SettingDefinition] = [
    SettingDefinition(
        key="EMPLOYEE_LIST_SORT",
        value_type=EmployerSetting.ValueType.TEXT,
        description=(
            "Which name the employee list, the attendance grid and the payroll run "
            "order sort on. One order everywhere, or the muscle memory is lost (D-16)."
        ),
        default="first_name",
        by_sector={DOMESTIC: "first_name", CONTRACT_CLEANING: "surname"},
    ),
    SettingDefinition(
        key="NIGHT_ALLOWANCE_PCT_OF_HOURLY",
        value_type=EmployerSetting.ValueType.NUMERIC,
        description=(
            "Percent of the hourly wage paid for each hour worked between the night "
            "work start and end times. BCEA s17(2) requires an allowance and sets no "
            "amount, so this is the employer's figure - EXCEPT in contract cleaning, "
            "where SD1 clause 16 gazettes 10 percent and the rule set is authoritative."
        ),
        # Decimal(0), not a figure: zero here means "nobody has set this", and the
        # night work calculator must treat it as unset rather than as "pay nothing".
        default=Decimal(0),
    ),
    SettingDefinition(
        key="NIGHT_ALLOWANCE_IS_STATUTORY",
        value_type=EmployerSetting.ValueType.BOOLEAN,
        description=(
            "True where the sector gazettes the allowance, so the rule set governs and "
            "the employer's own figure is ignored. Seeded True for contract cleaning."
        ),
        default=False,
        by_sector={CONTRACT_CLEANING: True},
    ),
    SettingDefinition(
        key="PAYSLIP_DELIVERY_PRINT",
        value_type=EmployerSetting.ValueType.BOOLEAN,
        description=(
            "Print and hand over payslips as well as emailing a secure link. A first "
            "class channel, not a fallback: BCEA s33 requires written particulars, and "
            "a link satisfies that for a smartphone and not at all for a feature phone "
            "with no data (D-14)."
        ),
        default=True,
    ),
    SettingDefinition(
        key="GROUP_HEADER_MINIMUM",
        value_type=EmployerSetting.ValueType.NUMERIC,
        description=(
            "Below this many employees the pay group headings are suppressed. Group "
            "headings over a two-row list are clutter, and that is the domestic "
            "employer's entire experience of the product (D-19)."
        ),
        # An int, and deliberately so. This is a count of employees for a UI
        # heading rule, not money and not a rate - the no-hard-coded-rate guard
        # flags Decimal and float literals precisely because those are what
        # statutory figures look like.
        default=4,
    ),
]

BY_KEY = {definition.key: definition for definition in SETTING_DEFINITIONS}


def _value_kwargs(definition: SettingDefinition, value) -> dict:
    """Put the value in the one typed column this setting's type names."""
    return {
        EmployerSetting.ValueType.TEXT: {"value_text": value},
        EmployerSetting.ValueType.NUMERIC: {"value_numeric": value},
        EmployerSetting.ValueType.BOOLEAN: {"value_boolean": value},
        EmployerSetting.ValueType.DATE: {"value_date": value},
    }[definition.value_type]


def seed_settings_for(employer) -> list[EmployerSetting]:
    """Create every setting this employer does not yet have, at its sector's default.

    Idempotent: a setting already present is left exactly as it is, because the
    employer may have changed it. Re-running after a new definition is added gives
    every existing employer the new setting without touching the ones they have set —
    which is how a registry entry added in P6 reaches employers onboarded in P3.
    """
    existing = set(
        EmployerSetting.objects.filter(employer=employer).values_list("setting_key", flat=True)
    )
    created = []
    for definition in SETTING_DEFINITIONS:
        if definition.key in existing:
            continue
        created.append(
            EmployerSetting.objects.create(
                tenant=employer.tenant,
                employer=employer,
                setting_key=definition.key,
                value_type=definition.value_type,
                set_by_employer=False,
                notes=definition.description,
                **_value_kwargs(definition, definition.default_for(employer.sector.code)),
            )
        )
    return created


def setting_value(employer, key: str):
    """One setting's value, or the registry default if the row is missing.

    Falls back rather than raising because a missing row means "onboarding has not
    seeded this yet", not "nobody knows" — unlike a statutory lookup, where a missing
    row genuinely means the system does not know the rate and must refuse.
    """
    definition = BY_KEY[key]
    row = EmployerSetting.objects.filter(employer=employer, setting_key=key).first()
    if row is None:
        return definition.default_for(employer.sector.code)
    return row.value
