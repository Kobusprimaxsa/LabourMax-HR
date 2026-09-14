"""The employee list: grouped by pay group, sorted the way this employer reads names.

Three decisions meet here, and they are easy to collapse into one another.

**D-16 — one order everywhere.** The employee list, the attendance grid and the
payroll run order all sort the same way, or the muscle memory built on one is wrong
on the next. That order is the employer's, held in the ``EMPLOYEE_LIST_SORT``
setting and seeded from the sector: a household calls their domestic worker by her
first name, a contract cleaning employer with sixty cleaners files by surname.

**D-133 — a user may re-sort their own list, and it is remembered.** That does not
break D-16, because it changes one person's view of one screen. The employer's
setting still governs the grid and the run, and it is still the default this user
gets until they choose otherwise.

**D-19 — no group headings over a short list.** Below ``GROUP_HEADER_MINIMUM``
employees the pay group headings are clutter, and a two-employee list is the
domestic employer's entire experience of the product.

The sort columns themselves are the generated, ICU-collated ``sort_name_first`` and
``sort_name_last`` (D-17) — lower-cased in the expression, so ``LIKE`` still uses
the index, and collated ``en-ZA-x-icu`` so *Van der Merwe* and *van der Merwe* sort
together and *Ää* lands where a South African reader expects it. Sorting on
``last_name`` in Python instead would be wrong in a way nobody notices until a
customer with an Afrikaans surname list says the order looks random.

Nothing here renders anything. The grouping, the order and the remembered choice
are the parts that have to be right and testable; the screen is UI work.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.managers import tenant_context_of
from core.models import TenantMembership
from employees.models import Employee
from employers.onboarding import setting_value

#: The two orders. The values are the ones ``EMPLOYEE_LIST_SORT`` is seeded with,
#: so an employer setting and a user preference are the same vocabulary.
FIRST_NAME = "first_name"
SURNAME = "surname"

#: Which stored column each order reads. Both are generated and ICU-collated.
SORT_COLUMNS = {FIRST_NAME: "sort_name_first", SURNAME: "sort_name_last"}

#: The only keys that may be written to ``tenant_membership.ui_preferences``, and
#: what each may hold. A bag with no registry becomes a place to put anything —
#: including, eventually, something that decides a figure (D-133).
UI_PREFERENCE_KEYS = {"employee_list_sort": frozenset(SORT_COLUMNS)}


class UnknownPreferenceError(Exception):
    """The key or the value is not in the registry. Nothing was written."""


@dataclass(frozen=True)
class PayGroupSection:
    """One pay group's block of the list."""

    pay_group_id: int | None
    heading: str
    employees: list[Employee]

    @property
    def count(self) -> int:
        return len(self.employees)


@dataclass(frozen=True)
class EmployeeList:
    """What the screen renders, and what a test can assert on."""

    sections: list[PayGroupSection]
    sort: str
    show_group_headings: bool

    @property
    def employees(self) -> list[Employee]:
        """Every employee, in list order, headings ignored."""
        return [employee for section in self.sections for employee in section.employees]

    @property
    def count(self) -> int:
        return sum(section.count for section in self.sections)


def default_sort(employer) -> str:
    """The employer's order — sector-derived at onboarding, theirs to change (D-16).

    Pins the tenant, and the reason is worth stating: ``employer_setting`` is
    tenant-scoped, so with nothing pinned the lookup returns no row, and
    ``setting_value`` then falls back to the registry default — quietly, and with
    the right answer most of the time, because the registry default IS the domestic
    default. The employer who changed their order would be the only one to see it
    ignored. A first draft of this function had exactly that bug and its test passed.
    """
    with tenant_context_of(employer):
        value = setting_value(employer, "EMPLOYEE_LIST_SORT")
    return value if value in SORT_COLUMNS else FIRST_NAME


def remembered_sort(membership: TenantMembership) -> str | None:
    """This user's own choice in this tenant, or None if they have not made one."""
    value = (membership.ui_preferences or {}).get("employee_list_sort")
    return value if value in SORT_COLUMNS else None


def remember_sort(membership: TenantMembership, sort: str) -> TenantMembership:
    """Store this user's choice. Refuses anything the registry does not know."""
    set_preference(membership, "employee_list_sort", sort)
    return membership


def set_preference(membership: TenantMembership, key: str, value) -> TenantMembership:
    """Write one registered preference key, leaving the rest of the bag alone."""
    permitted = UI_PREFERENCE_KEYS.get(key)
    if permitted is None:
        raise UnknownPreferenceError(
            f"{key!r} is not a registered UI preference. Add it to "
            f"UI_PREFERENCE_KEYS with the values it may take — a bag that accepts "
            f"anything is where a setting that decides a figure ends up (D-133)."
        )
    if value not in permitted:
        raise UnknownPreferenceError(
            f"{value!r} is not a permitted value for {key!r}. Expected one of {sorted(permitted)}."
        )

    with tenant_context_of(membership):
        preferences = dict(membership.ui_preferences or {})
        preferences[key] = value
        membership.ui_preferences = preferences
        membership.save(update_fields=["ui_preferences", "updated_at"])
    return membership


def resolve_sort(
    employer, *, membership: TenantMembership | None = None, sort: str | None = None
) -> str:
    """Which order to use: what was asked for, then the user's, then the employer's.

    The employer's setting is the floor rather than the ceiling — it is a default,
    not a policy, and it stays the one that governs the attendance grid and the
    payroll run whatever an individual has chosen for their own list (D-16).
    """
    if sort in SORT_COLUMNS:
        return sort
    if membership is not None:
        remembered = remembered_sort(membership)
        if remembered is not None:
            return remembered
    return default_sort(employer)


def employee_list(
    employer,
    *,
    membership: TenantMembership | None = None,
    sort: str | None = None,
    include_terminated: bool = False,
    remember: bool = False,
) -> EmployeeList:
    """The list, grouped and ordered. One query.

    ``include_terminated`` is off by default and is a display choice, not a data
    one: the rows are never deleted, and a terminated employee stays reachable for
    their IRP5 long after they stop appearing here.

    Grouping reads ``current_pay_group`` — the cache (D-18) — rather than joining
    ``employee_remuneration`` and date-filtering it on every render. That is the
    whole reason the column exists, and the reason the nightly job has to run:
    an employee whose new rate started this morning must already have moved into
    the right group by the time this is called.
    """
    order = resolve_sort(employer, membership=membership, sort=sort)
    if remember and membership is not None:
        remember_sort(membership, order)

    column = SORT_COLUMNS[order]

    with tenant_context_of(employer):
        rows = Employee.objects.filter(employer=employer)
        if not include_terminated:
            rows = rows.exclude(status__in=[Employee.Status.TERMINATED, Employee.Status.ARCHIVED])
        rows = list(
            rows.select_related("current_pay_group").order_by(
                # Group first, then the name within it. NULLs last: an employee
                # with no rate captured yet belongs at the bottom of the list
                # under "no pay group", not at the top above everybody paid.
                models_nulls_last("current_pay_group__name"),
                column,
            )
        )

        minimum = int(setting_value(employer, "GROUP_HEADER_MINIMUM") or 0)

    sections: list[PayGroupSection] = []
    for employee in rows:
        group = employee.current_pay_group
        group_id = group.pk if group else None
        if not sections or sections[-1].pay_group_id != group_id:
            sections.append(
                PayGroupSection(
                    pay_group_id=group_id,
                    heading=group.name if group else "No pay group",
                    employees=[],
                )
            )
        sections[-1].employees.append(employee)

    return EmployeeList(
        sections=sections,
        sort=order,
        show_group_headings=len(rows) >= minimum and len(sections) > 1,
    )


def models_nulls_last(field: str):
    """``ORDER BY <field> NULLS LAST``, without importing F at every call site."""
    from django.db.models import F

    return F(field).asc(nulls_last=True)
