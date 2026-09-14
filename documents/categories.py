"""The system document categories — sheet 03's three seeded lists.

Twenty-eight categories, shared exactly like ``payroll_component`` and
``leave_type``: one row each, no tenant, readable by every employer and
writable by none of them. A tenant that wants a category of its own — a
household's own house-rules acknowledgement, say — adds one alongside.

Three things are set only where sheet 03 actually says to, and nowhere else:

**``requires_expiry_date`` is TRUE for exactly five** — work permit, asylum
permit, police clearance, driver's licence and the COIDA letter of good
standing. Every other category defaults FALSE, including ones that might look
like they should expire (a training certificate, a POPIA consent) — sheet 03
does not say so, and inventing an expiry rule for a category the workbook is
silent on is exactly the kind of guess CLAUDE.md rules out.

**``visible_to_employee`` is TRUE for exactly one** — the signed employment
contract. D-44's whitelist means every other category, including ones added to
this tuple after today, stays hidden from self-service until somebody
deliberately turns the flag on.

**``sector`` is set for exactly two** — CIPC registration and the BEE
certificate, both restricted to CONTRACT_CLEANING, because sheet 03 names only
these two as "not household documents". Nothing else here is sector-restricted;
a company registration certificate or a COIDA registration could arguably be
argued sector-specific too, but the workbook does not say so and this module
does not decide compliance questions the workbook is silent on.

**Two seeded categories share a workbook description** — "bank confirmation"
and "proof of address" each appear once for the employer and once for the
employee. They need distinct codes because the unique is per (tenant, code)
and both are shared rows with a NULL tenant, so ``BANK_CONFIRMATION_EMPLOYER``/
``BANK_CONFIRMATION_EMPLOYEE`` and their proof-of-address counterparts are
this module's own naming, not the workbook's literal codes.

**``is_confidential_by_default`` is TRUE for exactly seven** — ID copy,
passport, work permit, asylum permit, police clearance, the employee's own
bank confirmation and the next-of-kin form (D-143). Sheet 03 does not make this
call either, but D-143 is not the same kind of silence as the expiry and
sector questions above: those ask the workbook to decide something it never
addresses, this asks whether a category the workbook already seeds happens to
carry POPIA special personal information or a third party's details, which is
a question this codebase can answer on its own facts. Police clearance is s26
special personal information (criminal behaviour); the identity documents
carry the ID number D-77 already encrypts at rest; the bank confirmation is
the ghost-employee fraud surface D-111 exists for — the EMPLOYEE's copy
(``BANK_CONFIRMATION_EMPLOYEE``), not the employer's own banking letter used
for CIPC and VAT filings, which is a company detail rather than personal
information; next-of-kin exposes a third party who never dealt with the
employer at all. CVs, qualifications, contracts and the registration letters
stay visible to read_only — none of them carry information a read_only user
should not see in the ordinary course of administering payroll.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from core.managers import platform_context
from documents.models import DocumentCategory

TENANT = DocumentCategory.AppliesTo.TENANT
EMPLOYER = DocumentCategory.AppliesTo.EMPLOYER
EMPLOYEE = DocumentCategory.AppliesTo.EMPLOYEE
WORKPLACE = DocumentCategory.AppliesTo.WORKPLACE
ANY = DocumentCategory.AppliesTo.ANY

CONTRACT_CLEANING_ONLY = "CONTRACT_CLEANING"


@dataclass(frozen=True)
class SystemCategory:
    code: str
    name: str
    applies_to: str
    sort_order: int
    requires_expiry_date: bool = False
    visible_to_employee: bool = False
    is_required_for_onboarding: bool = False
    is_confidential_by_default: bool = False
    sector_code: str | None = None


SYSTEM_CATEGORIES: tuple[SystemCategory, ...] = (
    # --------------------------------------------------- tenant / employer
    # Sheet 03 groups this list as "tenant/employer". Every one of these names
    # a specific registered entity — a CIPC number, a PAYE reference, a bank
    # account — and employers.Employer is where those live (registration_number,
    # income_tax_reference), including for a tenant running several employing
    # entities under one subscription. So applies_to=EMPLOYER throughout,
    # deliberately, rather than TENANT: a multi-employer tenant needs one CIPC
    # certificate per entity, not one shared across all of them.
    SystemCategory(
        "CIPC_REGISTRATION", "CIPC registration", EMPLOYER, 10, sector_code=CONTRACT_CLEANING_ONLY
    ),
    SystemCategory(
        "COMPANY_REGISTRATION_CERTIFICATE", "Company registration certificate", EMPLOYER, 20
    ),
    SystemCategory("VAT_REGISTRATION", "VAT registration", EMPLOYER, 30),
    SystemCategory("PAYE_UIF_SDL_LETTER", "PAYE/UIF/SDL registration letters", EMPLOYER, 40),
    SystemCategory("COIDA_REGISTRATION", "COIDA registration", EMPLOYER, 50),
    SystemCategory(
        "COIDA_GOOD_STANDING",
        "COIDA letter of good standing",
        EMPLOYER,
        60,
        requires_expiry_date=True,
    ),
    SystemCategory(
        "BEE_CERTIFICATE", "BEE certificate", EMPLOYER, 70, sector_code=CONTRACT_CLEANING_ONLY
    ),
    SystemCategory(
        "BANK_CONFIRMATION_EMPLOYER", "Bank confirmation", EMPLOYER, 80
    ),  # the company's own banking letter, not personal information
    SystemCategory("PROOF_OF_ADDRESS_EMPLOYER", "Proof of address", EMPLOYER, 90),
    SystemCategory("DIRECTOR_ID", "Director ID", EMPLOYER, 100),
    # ------------------------------------------------------------- employee
    SystemCategory("ID_COPY", "ID copy", EMPLOYEE, 200, is_confidential_by_default=True),
    SystemCategory("PASSPORT", "Passport", EMPLOYEE, 210, is_confidential_by_default=True),
    SystemCategory(
        "WORK_PERMIT",
        "Work permit",
        EMPLOYEE,
        220,
        requires_expiry_date=True,
        is_confidential_by_default=True,
    ),
    SystemCategory(
        "ASYLUM_PERMIT",
        "Asylum permit",
        EMPLOYEE,
        230,
        requires_expiry_date=True,
        is_confidential_by_default=True,
    ),
    SystemCategory("CV", "CV", EMPLOYEE, 240),
    SystemCategory("QUALIFICATIONS", "Qualifications", EMPLOYEE, 250),
    SystemCategory("DRIVERS_LICENCE", "Driver's licence", EMPLOYEE, 260, requires_expiry_date=True),
    SystemCategory(
        "POLICE_CLEARANCE",
        "Police clearance",
        EMPLOYEE,
        270,
        requires_expiry_date=True,
        is_confidential_by_default=True,
    ),
    SystemCategory(
        "BANK_CONFIRMATION_EMPLOYEE",
        "Bank confirmation",
        EMPLOYEE,
        280,
        is_confidential_by_default=True,
    ),  # D-111's ghost-employee fraud surface
    SystemCategory(
        "EMPLOYMENT_CONTRACT",
        "Signed employment contract",
        EMPLOYEE,
        290,
        visible_to_employee=True,
        is_required_for_onboarding=True,
    ),
    SystemCategory("TRAINING_CERTIFICATE", "Training certificates", EMPLOYEE, 300),
    SystemCategory("PROOF_OF_ADDRESS_EMPLOYEE", "Proof of address", EMPLOYEE, 310),
    SystemCategory(
        "NEXT_OF_KIN_FORM",
        "Next-of-kin form",
        EMPLOYEE,
        320,
        is_confidential_by_default=True,
    ),
    SystemCategory(
        "POPIA_CONSENT", "POPIA consent", EMPLOYEE, 330, is_required_for_onboarding=True
    ),
    # ------------------------------------------------------------ workplace
    SystemCategory("CLIENT_CONTRACT", "Client contract", WORKPLACE, 400),
    SystemCategory("SITE_INSTRUCTION", "Site instruction", WORKPLACE, 410),
    SystemCategory("SAFETY_FILE", "Safety file", WORKPLACE, 420),
)


class CategorySeedError(Exception):
    """The catalogue cannot be seeded. Nothing was written."""


def _sectors(needed: set[str]):
    from statutory.models import Sector

    found = {s.code: s for s in Sector.objects.filter(code__in=needed)}
    missing = sorted(needed - set(found))
    if missing:
        raise CategorySeedError(
            f"Sector(s) {missing} are not loaded, so the document categories that "
            f"restrict themselves to one cannot be created. Run "
            f"`python manage.py loadstatutory --all` first."
        )
    return found


def seed_system_categories() -> list[DocumentCategory]:
    """Create any system category that does not exist yet. Returns what was created.

    Idempotent, and deliberately never updates. A category already in the
    catalogue is left exactly as it is: the system-row lock would refuse the
    write in any case, and a tenant may already have documents filed against it.

    ``transaction.atomic()`` is load-bearing and must wrap ``platform_context()``
    rather than the other way round — ``set_config(..., true)`` is
    transaction-local, and under autocommit (every management command) the flag
    is gone before the INSERT that needs it (D-92).
    """
    wanted = {c.sector_code for c in SYSTEM_CATEGORIES if c.sector_code}
    sectors = _sectors(wanted)

    created: list[DocumentCategory] = []
    with transaction.atomic(), platform_context():
        existing = set(DocumentCategory.objects.shared().values_list("code", flat=True))

        for spec in SYSTEM_CATEGORIES:
            if spec.code in existing:
                continue

            category = DocumentCategory(
                tenant=None,
                code=spec.code,
                name=spec.name,
                applies_to=spec.applies_to,
                sector=sectors[spec.sector_code] if spec.sector_code else None,
                requires_expiry_date=spec.requires_expiry_date,
                is_required_for_onboarding=spec.is_required_for_onboarding,
                visible_to_employee=spec.visible_to_employee,
                is_confidential_by_default=spec.is_confidential_by_default,
                sort_order=spec.sort_order,
                is_system=True,
            )
            category.full_clean(exclude=["tenant"])
            category.save()
            created.append(category)

    return created


def category(code: str) -> DocumentCategory:
    """Fetch one category by code, shared or the current tenant's own.

    Raises ``DocumentCategory.DoesNotExist`` rather than returning None: a
    document filed against no category is not a thing that should be
    constructible.
    """
    return DocumentCategory.objects.get(code=code)
