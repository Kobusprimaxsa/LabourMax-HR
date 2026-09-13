# Labourmax-HR

A multi-tenant HR and payroll platform for the South African **domestic worker** and
**contract cleaning** sectors. Employers self-register, subscribe, and run compliant
payroll without a bureau and without a payroll specialist on staff.

Read this file before doing anything. The rules below are not style preferences —
each one exists because of a specific way payroll systems go wrong.

**Authoritative specifications** (kept outside the repo, in the project documents):

- `Labourmax-HR_Development_Plan.docx` — architecture, calculation engines, phases, decisions
- `Labourmax-HR_Database_Specification.xlsx` — 107 tables, 1,445 columns, DDL level
  - Sheet 02 is the column dictionary. **It is the source of truth for every model.**
  - Sheet 09 is the decision register. Read it before proposing a change to a settled decision.

`docs/PHASES.md` in this repo is the work breakdown. `docs/DECISIONS.md` is the register
summary. If this file and the workbook disagree, **the workbook wins** — and tell Kobus.

---

## Non-negotiables

### 1. Tenant isolation — three layers, no exceptions

No table holding employer or employee data may be merged without **all three**:

1. `tenant_id` column, and the model inherits `TenantScopedModel`
2. A PostgreSQL row-level security policy (see `core/db/rls.py`)
3. A passing cross-tenant isolation test — generated automatically from the model
   registry by `core/tests/test_tenant_isolation.py`, so a new model is covered the
   day it is written

One employer seeing another employer's employees is the failure that ends this product.
It is the only defect that cannot be apologised for. A single ORM filter is not enough.

**Two bases, and the choice is deliberate** (decisions D-52, D-53):

| | `TenantScopedModel` | `TenantOptionalModel` |
|---|---|---|
| `tenant_id` | NOT NULL | nullable |
| For | employer and employee data | security and operations records that predate the tenant being known |
| Tenant session sees | its own rows | its own rows, **never** the NULL-tenant ones |
| Session with no tenant pinned sees | nothing | the NULL-tenant rows |
| Platform console sees | nothing — it reads through a tenant context, not around it | everything, inside `platform_context()` only |
| Migration calls | `enable_rls(table)` | `enable_rls_optional(table)` |

A model that grows a `tenant` field while inheriting **neither** base is caught by
`test_no_model_carries_a_tenant_column_without_a_base`. Do not work around that test
by deleting it; pick a base.

`platform_context()` is the **only** way to read across tenants. It lifts the manager
filter and the RLS policy together, logs every entry, and is named so it is obvious in
review and trivial to grep. Every use of it in employer-facing code is a bug. A request
never gets it.

**Layer 1 and layer 2 must agree.** `TenantOptionalManager` mirrors
`enable_rls_optional()` clause for clause. Change one and you must change the other in
the same commit, or the disagreement surfaces as a baffling empty result rather than as
a test failure.

**Migrations call `core/db/rls.py`, and migrations are replayed from zero on every
test run.** So changing a signature there breaks every past migration that calls it —
`0001_initial` included. Grep for the function name and update every call site in the
same commit. Keeping the helpers shared is a deliberate trade: the policy SQL stays in
one place and cannot drift between tables, at the cost of this one rule to remember.
Editing an applied migration is otherwise not allowed — a policy *change* gets a new
migration that re-applies (see `0002_harden_rls_casts`); only a call that would no
longer import gets edited in place.

**Every service function that touches a tenant-scoped row must pin the tenant.**
Use `tenant_context_of(instance)` for a row you were handed, or `tenant_context(id)`
when you only have the id. This has bitten this codebase four times, and the reason it
keeps happening is that the failure mode depends on how you asked:

| How you queried | What RLS does with no tenant pinned |
|---|---|
| `count()` / `filter()` | returns 0 rows — reads as "none", not "not allowed" |
| `save(update_fields=[...])` | raises "Save with update_fields did not affect any rows" |
| plain `save()` | **silently writes nothing and reports success** |
| `INSERT` | "new row violates row-level security policy" |

Only two of those four tell you what is actually wrong. `all_tenants` does not help —
it bypasses the manager, not the policy. A seat-limit check bitten by this returned
0 seats in use and waved through every request. When a count comes back suspiciously
zero, or a save appears to do nothing, check the context before you check the data.

A scanner callback, a Celery task and a retention job all arrive with no request and
therefore no tenant context, so this is the ordinary path, not an edge case.

**Two PostgreSQL behaviours that will cost you an afternoon if you forget them:**

- `''::bigint` **raises**, it does not evaluate false, and SQL gives no
  left-to-right evaluation guarantee — so a guard clause does not reliably protect a
  cast sitting next to it. Policies use `nullif(current_setting(...), '')::bigint`
  so an unpinned session returns no rows instead of erroring.
- Django appends `RETURNING id` to every INSERT, and PostgreSQL applies a policy's
  **USING** clause to rows returned that way. A row the USING clause rejects
  therefore cannot be inserted at all, reported as the misleading
  "new row violates row-level security policy". A write-only-never-readable row is
  not achievable through the ORM.

**Never point the application at a PostgreSQL superuser.** A superuser bypasses row-level
security, so every RLS assertion would pass with the protection absent. `manage.py dbcheck`
and the isolation suite both refuse outright if the connected role is one.

### 2. Anything that changes over time is a row, not a field

Pay rates, positions, bank accounts, tax profiles, work schedules, minimum wages, PAYE
brackets, leave rules — all carry `effective_from` / `effective_to`. Changing a value
**inserts a row and closes the previous one**. It never overwrites.

A payroll run for March 2026 must calculate identically when re-run in 2029.

### 3. Balances are derived, never stored as editable numbers

Leave balances come from `leave_transaction`, an append-only ledger. Year-to-date figures
come from finalised payslips. Both have a materialised cache; both caches can be discarded
and rebuilt from source rows. Never "fix" a balance by writing to it.

### 4. Finalised financial records are immutable

Once a payroll run is finalised, its payslips and lines are never updated or deleted.
A correction creates a **reversing** payslip plus a replacement in a new run. Same for
issued invoices (credit note) and leave transactions (reversal).

### 5. Every calculation writes its own evidence

`payroll_calculation_trace` stores, per payslip per calculator: the inputs, the primary
keys of every statutory row read, the outputs, and any warnings. When an employee disputes
a figure from eighteen months ago, the answer is a stored record — not a re-run of today's
code against today's rates.

### 6. Money is `Decimal`, and rounding happens in exactly one place

Every monetary column is `NUMERIC`. **No floating point anywhere, ever.** Intermediate
values carry 4–6 decimal places and are stored unrounded alongside the rounded figure.
Rounding to 2 decimals, `ROUND_HALF_UP`, happens at the payslip line and nowhere else.

```python
from decimal import Decimal, ROUND_HALF_UP

amount = (units * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

If you see `float` anywhere near money, it is a bug.

### 7. Documents and payslips freeze what they showed

A payslip stores `employee_snapshot` — name, employee number, position, rate, bank
reference as at finalisation. A generated contract stores its merge data. Reprinting a
2027 payslip after the employee married and changed banks must reproduce the **original**.

---

## The calculators rule

`calculators/` contains **pure functions only**.

- No ORM imports. No database access. No file I/O. No `datetime.now()`.
- Every function takes a frozen input structure and returns a result structure.
- The caller assembles inputs by reading effective-dated rows; the calculator only computes.

This is what makes the payroll engine testable against SARS worked examples without a
database, and what stops calculation logic leaking into views and models where it becomes
impossible to verify.

**100% branch coverage on `calculators/`. Nothing less ships.**

A calculator without a golden-file test — reproducing a published SARS or DEL worked
example exactly — does not ship either.

---

## Statutory data

**Never hard-code a rate, threshold, multiplier or leave rule. Anywhere. Ever.**

Every statutory number lives in an effective-dated table with a gazette citation and a
source URL. The annual March change is then a data load, not a release.

Current seeded values (effective 1 March 2026 unless noted) are on sheet 05 of the workbook.

Things that are easy to get wrong, and have been got wrong before:

- **UIF ceiling** moves on ministerial notice, on **no fixed calendar** — independent of the
  tax year. It is the parameter most often missed in South African payroll.
- **SDL exemption** (R500,000) is **forward-looking** — it asks whether the employer
  reasonably believes payroll will exceed it over the next twelve months. It cannot be
  computed from history. It is a human-set flag.
- **Director PAYE**: the flat 25% rate was repealed in 2017. Directors use the ordinary tables.
- **PAYE rounding**: SARS sanctions two legitimate methods that are *expected* to differ
  slightly. A small variance against another package is not automatically a bug.
- **BCEA earnings threshold**: a high earner loses only s18(3), **not all of s18** — they keep
  the public holiday pay entitlement.
- **Commission is excluded** from the UIF contribution base. Bonuses are not.
- **Bonuses in PAYE**: annualise regular pay ×12, then add the annual payment **once**.
  Multiplying a bonus by twelve is the classic December over-deduction.
- **Parental leave** is under an interim Constitutional Court reading-in (4 months + 10 days,
  shareable). Held as effective-dated data so remedial legislation is a data load.

`reference_data_version.data_current_through` is the **staleness guard**: a payroll run
whose period ends beyond it raises a *blocking* validation issue rather than silently
falling back to superseded rates.

---

## Conventions

| Thing | Rule |
|---|---|
| Table names | Singular, snake_case: `employee`, not `employees` |
| Primary keys | `BigAutoField` surrogate on every table. Natural keys become unique constraints |
| External ids | Anything in a URL or API carries `public_uid` (UUID). **Never expose integer ids** |
| Foreign keys | `<target>_id`. `PROTECT` by default; `CASCADE` only for genuinely owned children |
| Audit columns | `created_at`, `created_by_user_id`, `updated_at`, `updated_by_user_id` via `AuditMixin` |
| Soft delete | Employees, employers, files — statutory retention outlasts the customer relationship |
| Ledger rows | Never deleted at all. Corrections insert reversals |
| Timestamps | `TIMESTAMPTZ`, stored UTC, rendered `Africa/Johannesburg` |
| Encrypted fields | ID numbers and bank account numbers, with `_last4` in a separate plain column |
| Booleans | Positive assertions: `is_active`, never `is_not_active` |
| Enumerations | `TextChoices` + a `CHECK` constraint. Not integer codes — raw SQL against production should read |

---

## Application structure

```
core/           tenancy, users, roles, audit, files, base models
billing/        plans, price bands, subscriptions, service agreements, invoices, gateway
statutory/      effective-dated reference data + loader + watch list
employers/      employer, workplaces, pay groups, payroll components
employees/      master file, engagements, remuneration, schedules
attendance/     daily capture, import, timesheet aggregation
leave/          types, cycles, ledger, applications, accrual engine
payroll/        periods, runs, payslips, YTD, terminations
calculators/    pure calculation functions — no ORM, no I/O
statutory_out/  EMP201, UI-19, IRP5, COIDA, bank files
discipline/     cases, actions, evidence
documents/      filing, categories, templates, generation, secure links
selfservice/    employee portal
console/        Labourmax superuser console
reporting/      report definitions and generation
```

Apps map one-to-one onto the twelve schema domains, so module boundaries are visible in
the directory listing.

---

## Stack

Python 3.14 (dev machine) · Django 5.2 LTS · **PostgreSQL 18** (13+ supported) · DRF · Celery + Redis ·
Django templates with HTMX and Alpine.js · WeasyPrint for PDFs · S3-compatible storage.

PostgreSQL is settled (decision D-24) and not revisitable — row-level security, exclusion
constraints on effective-dated ranges, native range partitioning, indexed JSONB.
The MySQL equivalents in the workbook are reference only.

---

## Commands

```powershell
# activate the virtualenv (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

python manage.py runserver
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser

pytest                              # everything
pytest core/tests/test_tenant_isolation.py -v   # the one that must never fail
pytest --cov=calculators --cov-report=term-missing --cov-fail-under=100
ruff check . && ruff format .
```

---

## Working style

- **Design then execute.** Agree the approach before writing code. Scope discipline matters
  more than speed on a build this long.
- **Verify.** Run the tests. Show the output. Do not report something as done that has not run.
- **Small commits**, one concern each, with the phase reference in the message: `P0: add TenantScopedManager`.
- **Never** change a settled decision (sheet 09) without raising it explicitly first.
- **Never** invent a statutory figure. If you need one and cannot cite it, say so and stop.
- Every production defect gets a **failing test before it gets a fix**.

## Current state

**P0 — Foundation & Tenancy: COMPLETE** (13 September 2026). 124 tests green locally and on
CI. Tenant isolation proven at all three layers, field-level audit trail with sensitive-value
masking, the administrative seat limit enforced by trigger, and file storage behind a
virus-scan gate.

**Next: P2 — Statutory Reference Data.** Not P1. Nothing in the payroll engine can be tested
against a SARS worked example until the reference tables exist, and a wrong UIF ceiling
blocks a pilot employer in a way billing does not. P1 also carries the most open decisions
(O-03 rand amounts, O-04 payment gateway), so starting it means stopping to ask.

See `docs/PHASES.md` for the task breakdown.

Nothing is deployed. There is no customer data. This is the right moment to be rigid
about the invariants above, because retrofitting any of them later is a rewrite.
