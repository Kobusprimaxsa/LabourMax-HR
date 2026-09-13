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

**Three bases, and the choice is deliberate** (decisions D-52, D-53, D-87):

| | `TenantScopedModel` | `TenantOptionalModel` | `TenantSharedModel` |
|---|---|---|---|
| `tenant_id` | NOT NULL | nullable | nullable |
| A NULL means | — | belongs to the platform, **no tenant may see it** | **available to all** |
| For | employer and employee data | security and operations records that predate the tenant being known | a catalogue the platform stocks and tenants extend |
| Tenant session reads | its own rows | its own rows, **never** the NULL-tenant ones | its own rows **and** the shared ones |
| Tenant session writes | its own rows | its own rows | its own rows only — shared rows are read-only to it |
| Session with no tenant pinned sees | nothing | the NULL-tenant rows | the shared rows |
| Platform console sees | nothing — it reads through a tenant context, not around it | everything, inside `platform_context()` only | everything, inside `platform_context()` only |
| Migration calls | `enable_rls(table)` | `enable_rls_optional(table)` | `enable_rls_shared(table)` |

**The two nullable bases mean opposite things by the same NULL.** Reusing the optional
base for `payroll_component` would have made the sixteen system components invisible to
every employer; flipping its clause instead would have opened every platform audit row to
every tenant. `test_no_model_inherits_two_tenant_bases` keeps them apart.

A model that grows a `tenant` field while inheriting **none** of them is caught by
`test_no_model_carries_a_tenant_column_without_a_base`. Do not work around that test
by deleting it; pick a base.

**The shared base is only for data that is not employer or employee data.** The test is
whether a row with no tenant would be safe on a competitor's screen — a component
definition is; anything with a person or an amount in it is not. A leak there is one no
policy would report, because the policy would be doing exactly what it was asked to.

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

**And a context block that WRITES outside a request must open a transaction first**
(D-92). `set_config(..., true)` is transaction-local, deliberately — a pooled connection
must not carry one request's tenant into the next. Under autocommit, which is where every
management command, Celery task and shell session runs, each statement is its own
transaction: the flag is set, the next statement commits, and the flag is gone before the
write, which then fails with "new row violates row-level security policy". A request never
hits this because `ATOMIC_REQUESTS` holds one transaction open; a test never hits it
because pytest-django wraps each one. So a whole green suite can sit on top of a command
that cannot run — which is exactly what happened to `seedcomponents`.

```python
with transaction.atomic(), platform_context():  # atomic FIRST
    ...
```

Test it with `@pytest.mark.django_db(transaction=True)`, or the wrapper hides it again.

**An abstract base's `Meta.constraints` is NOT inherited** by a child that declares its
own `Meta` — and every model here declares one, for `db_table`. Put a CHECK on an abstract
base and it silently does not exist, while the base class makes the code read as though it
does. Statutory constraints are therefore explicit per model via the helpers in
`statutory/models.py`, with `statutory/tests/test_citations.py` reading `pg_constraint` to
prove they landed.

**`NULL = NULL` is unknown in PostgreSQL, so nullable key columns escape both unique and
exclusion constraints.** A `UniqueConstraint` over nullable scope columns permits duplicates,
and an `ExclusionConstraint` permits overlaps, precisely for the rows where every scope
column is NULL — which in `minimum_wage_rate` is the general National Minimum Wage, the row
most likely to be loaded twice. Use `nulls_distinct=False` and `Coalesce(col, 0)`.

**Extensions belong in migrations, not in a setup script.** `BtreeGistExtension()`,
`CITextExtension()`, `CryptoExtension()`. pytest rebuilds the test database from `template1`
on every run, so an extension installed by hand into one database is how a constraint comes
to exist in development and be missing in production. All three are trusted in PostgreSQL
13+, so the non-superuser application role can create them.

**FORCE RLS subjects the FOREIGN KEY check to the policy.** This is the worst one, because
three layers fail together and none raises. Deleting a `sector` row from a session with no
tenant pinned succeeds and orphans every `employer` that referenced it: Django's `PROTECT`
collector finds no referencing rows (RLS hides them), PostgreSQL's own FK check finds none for
the same reason, and `platform_context()` does not help because strict tenant tables carry no
platform override. Reference tables that tenant tables point at therefore carry a
**BEFORE DELETE trigger** — `no_delete()` in `core/db/rls.py` — which runs regardless of what
the deleting session can see (D-76).

**Three PostgreSQL behaviours that will cost you an afternoon if you forget them:**

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

The loaded figures live in `reference/ref-2026.03.01.json`, built by
`tools/build_reference_fixture.py`, with a citation on every row. Sheet 05 of the workbook
is the earlier draft of the same thing and is **not** the source: where the two disagree,
the gazette or the SARS table wins and the workbook gets corrected.

`manage.py checkstatutory` reconciles the loaded data against itself — every PAYE base
against the bands below it, every tax threshold against its rebates. `verifystatutory`
refuses to record verification while anything blocking is outstanding.

**A clone of this repo has empty reference tables.** The fixtures in `reference/` must be
loaded before anything works, and `checkstatutory` refuses rather than reporting success on an
empty database (D-74) — every check iterates rows, so over no rows they all pass.

```powershell
python manage.py loadstatutory --all
python manage.py seedcomponents
```

**Never load them with a shell glob.** Alphabetical order puts the rule set fixtures before
the file that creates the sectors they reference, and the loader refuses them (D-75). `--all`
uses `FIXTURE_ORDER` in `statutory/loader.py`, which is dependency order.

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
- **Parental leave** is under an interim Constitutional Court reading-in — *Van Wyk v Minister
  of Employment and Labour* [2025] ZACC 20, 3 October 2025: four months and ten days for all
  parents together, divided as they agree and split as equally as possible if they cannot.
  The declaration of invalidity is suspended 36 months for Parliament to legislate, so this
  **will** change. Stored as months plus days, never as a day count.
- **The maternity restriction runs after the birth, not before it.** A birth mother may not
  work for six weeks after giving birth unless certified fit; separately, she may start leave
  up to four weeks before. Two different rules, two columns.
- **Domestic workers depart from the BCEA in three places** (SD7): five days' family
  responsibility leave rather than three, fifteen hours' overtime a week rather than ten, and
  four weeks' notice from six months' service rather than two.
- **Contract cleaning departs in three others** (SD1): a gazetted night allowance of 10% of the
  hourly wage between 18:00 and 06:00 — the only sector with a real figure in that column; a
  short-day minimum of **six** hours rather than four; and the 4,333-week December bonus,
  pro-rated as full calendar months over twelve on termination.

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
| Encrypted fields | ID numbers and bank account numbers, with `_last4` in a separate plain column. Needs `FIELD_ENCRYPTION_KEY` — without it every write refuses (D-77), and `manage.py check` says so in one line (D-94) |
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
python manage.py dbcheck            # connection, extensions, and the superuser refusal

python manage.py loadstatutory --all        # dependency order; never a shell glob
python manage.py loadstatutory reference/ref-2026.03.01.json --loaded-by you@example.com
python manage.py verifystatutory REF-2026.03.01 --verified-by someone-else@example.com `
    --current-through 2027-02-28 --golden-tests-passed
python manage.py seedcomponents             # the shared payroll component catalogue

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

**P3 — Employer Setup: tables complete** (13 September 2026). 486 tests green. `employer`,
`employer_statutory_registration` and `employer_bank_account` exist, tenant-scoped and picked
up automatically by the generated isolation suite. `core/db/fields.py` brings the first
encrypted column in the schema, unsearchable by design (D-77). Two structural findings came
out of it: FORCE RLS defeats foreign key enforcement (D-76) and the reference tables now refuse
DELETE by trigger.

`workplace` and `employer_setting` are in too. A workplace's wage area is derived from its
municipality and stored with the date it was read (D-81) — and `municipality_area_map` is
**empty**, so no contract cleaning workplace resolves an area today. That is a data-sourcing
job, and until it is done onboarding a contract cleaning employer stops at the address.

`pay_group` and the period generator are in, built to sheet 02 of the workbook (D-82 settled a
design question against Claude's proposal — the workbook wins). A period belongs to the tax year
its **payment date** falls in, not its period end (D-83).

`payroll_component` closes the phase, and it brought two structural things with it. It is the
first **shared** table — a NULL tenant means *available to all*, which no existing base could
express, so `TenantSharedModel` and `enable_rls_shared()` are the third tenancy shape (D-87).
And none of its sixteen system components carries a rate: `OT_1_5` is a label, and the 1.5
lives in `working_time_rule_set` with its citation (D-88). The four base flags are copied from
the SARS source code and `clean()` refuses any other combination, so "is this taxable" is still
decided in one place (D-89).

`SEVERANCE` ships **inactive** — source code 3901 is not loaded and severance is taxed on a
directive, so pointing it at 3601 would be wrong on the IRP5 and wrong on the rate (D-90).

```powershell
python manage.py seedcomponents          # after loadstatutory --all; idempotent
python manage.py seedcomponents --list   # the catalogue, without touching the database
```

Still open in P3: the municipality-to-area data.

**P2 — Statutory Reference Data: structure complete, data loaded, awaiting
verification** (13 September 2026). 351 tests green. All twenty tables exist with their
constraints; `statutory/resolve.py` is the only place that answers "what applied on this date";
the loader refuses any file with an uncited row and never updates an existing one;
`loadstatutory` and `verifystatutory` are two commands because loading and verifying are two
people; and the no-hard-coded-rate rule runs as a test on every commit rather than as a grep
somebody remembers.

`reference/ref-2026.03.01.json` holds 61 rows and `reference/ref-2026.03.01-rules.json` six more, researched from primary sources: the 1 March 2026
wage floors, the SARS 2027 tax year tables, the contribution parameters, public holidays for
2026 and 2027, the maintenance calendar, the BCEA, domestic and contract cleaning rule sets, 19 SARS source codes with cited base flags, and 25 banks. It is loaded and reconciles, and it is **not
verified** — `in_force_on()` cannot see it, so every payroll run is still blocked. That is
correct and deliberate.

**What remains in P2:**

- Kobus verifies every figure against its source document, then `verifystatutory`
- The notice band gap (D-68): SD1 splits notice at four weeks of service and the rule set
  table cannot express it. Fix before P6 builds the termination engine
- Contract cleaning **Area C (KwaZulu-Natal)** has no rate: the gazette states none and points
  at the BCCCI collective agreement, which nobody has. A KwaZulu-Natal contract cleaning
  employer cannot be onboarded until it is loaded
- Account number lengths per bank (D-72). NULL today; they come from the banks or from the
  EFT specification the payment partner supplies, and a guessed bound stops someone being paid
- The golden tests, which are what finally allows `golden_tests_passed`

Chosen ahead of P1 because nothing in the payroll engine can be tested against a SARS worked
example until the reference tables exist, and a wrong UIF ceiling blocks a pilot employer in a
way billing does not. P1 also carries the most open decisions (O-03 rand amounts, O-04 payment
gateway), so starting it means stopping to ask.

**Statutory figures are never invented.** If a rate is needed and cannot be cited, say so and
stop. That applies to filling in a fixture as much as to writing code.

See `docs/PHASES.md` for the task breakdown.

Nothing is deployed. There is no customer data. This is the right moment to be rigid
about the invariants above, because retrofitting any of them later is a rewrite.
