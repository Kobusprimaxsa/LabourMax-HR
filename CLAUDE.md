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
| plain `save()` on a NEW row | **silently writes nothing and reports success** |
| plain `save()` on an EXISTING row | "new row violates row-level security policy" — on an **INSERT**. The UPDATE matched zero rows, Django read that as "not in the table yet" and fell back to inserting with the pk set. The message names neither the update nor the missing context |
| `refresh_from_db()` | `DoesNotExist`, for a row you are holding in your hand |
| `INSERT` | "new row violates row-level security policy" |

Only two of those six tell you what is actually wrong. `all_tenants` does not help —
it bypasses the manager, not the policy. A seat-limit check bitten by this returned
0 seats in use and waved through every request. When a count comes back suspiciously
zero, or a save appears to do nothing, check the context before you check the data.

A scanner callback, a Celery task and a retention job all arrive with no request and
therefore no tenant context, so this is the ordinary path, not an edge case.

**A service function that reads a setting is one of them.** `default_sort()` looked
up `employer_setting` with nothing pinned, got no row, and fell back to the registry
default — which IS the domestic default, so it was right for most employers and
silently wrong for exactly the one who had changed their order. Its first test
passed. When a fallback and the truth agree by coincidence, a missing tenant context
does not announce itself at all.

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
installed by `statutory/0002` and backing every `ExclusionConstraint` in this schema — the
only extension this codebase actually uses. pytest rebuilds the test database from
`template1` on every run, so an extension installed by hand into one database is how a
constraint comes to exist in development and be missing in production. It is trusted in
PostgreSQL 13+, so the non-superuser application role can create it. `manage.py dbcheck`'s
`REQUIRED_EXTENSIONS` names only what a migration has installed (D-137) — it is not a
wishlist. `CITextExtension()` and `CryptoExtension()` are not required: hashing and
encryption are Python-side (`hashlib`, `hmac`, `Fernet`) because D-77 keeps the key out of
the database, and D-98 abandoned `CITEXT`. Something needing either later adds the
`Extension()` operation to the migration that needs it, and `REQUIRED_EXTENSIONS` grows
with it — not ahead of it.

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

### Windows traps

pytest's default `basetemp` lives under `C:\Users\<name>\AppData\Local\Temp\pytest-of-<name>`.
When the account name contains a space, or a prior run left that directory in a bad ACL
state, it can end up locked such that even `icacls` is denied — every test using the
`tmp_path` fixture then fails at fixture setup, as dozens of unrelated `PermissionError`s
that look like a code problem and are not. `pytest.ini` pins `--basetemp=.pytest-tmp` for
exactly this reason; if it still happens, point `--basetemp` at a directory you control
rather than fighting the ACL.

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

**P3 — Employer Setup: tables complete** (13 September 2026). `employer`,
`employer_statutory_registration` and `employer_bank_account` exist, tenant-scoped and picked
up automatically by the generated isolation suite. `core/db/fields.py` brings the first
encrypted column in the schema, unsearchable by design (D-77). Two structural findings came
out of it: FORCE RLS defeats foreign key enforcement (D-76) and the reference tables now refuse
DELETE by trigger.

`workplace` and `employer_setting` are in too. A workplace's wage area is derived from its
municipality and stored with the date it was read (D-81). `municipality_area_map` now holds
the **twelve municipalities the determination itself names** (D-118) — and D-118 also corrected
the area lettering that D-61 got wrong: Area A is the listed Metropolitan **and** Local Councils
in one column, Area B is all of KwaZulu-Natal with no gazetted figure (BCCCI rates), and Area C
is the residual "rest of the RSA" where most workplaces land. The table is not a gazetteer and
must not become one.

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

**P4 — Employee Master File: COMPLETE** (14 September 2026). 824 tests green. `employee`,
`employee_address`, `employee_contact`, `employee_engagement`, `employee_position` and
`employee_remuneration`, `work_schedule`, `work_schedule_day`, `employee_tax_profile`,
`employee_bank_account`, `employee_leave_entitlement`, `employee_recurring_component` and
`employee_note` are in, and P4's definition of done is met in full: a rate below the
sectoral minimum raises with both figures and the gazette citation, a future-dated
increase flips the pay cache on its own effective date, and forty employees import
from a spreadsheet with every one of those checks applied — the batch reverses as a
unit. Two things on the employee table are worth knowing before touching it.

`id_number_hash` is a keyed HMAC **scoped to the tenant** (D-95). Unscoped, the same
person hashes identically everywhere, so anyone holding the column can join two
subscribers' employee tables and learn that one household's domestic worker also works
for another. Nothing decrypts and no row is read that should not be — which is why the
isolation suite cannot catch it, and why `employer_bank_account` was retrofitted with the
same scope in the same commit.

The ID number is verified by checksum **and** by cross-checking the date of birth against
the number's own first six digits (D-96). The checksum catches a mistyped digit; only the
cross-check catches a number that is internally perfect and belongs to somebody else. The
century is never guessed — `employees/identity.py` returns both readings and lets the
captured date decide.

The two sort columns are generated, lower-cased in the expression, and carry
`en-ZA-x-icu` **set in the creating migration** (D-17) — changing a collation later
rewrites the table and every index on it.

A re-hire is a **new engagement row**, never an edited first one (D-103). Service length
is read from `employee_engagement`, so editing would hand a returning employee notice,
leave and severance they never earned, all of it plausible on screen.

**The BCEA s43 minimum age is reference data, not a literal** (D-100). It is a statutory
threshold, and `test_no_hardcoded_rates` would not have caught a literal `15` — that test
scans for `Decimal` and `float`, and an age is an `int`. This is the first case where the
rule and the automated guard come apart, so read the rule rather than trusting the test.
And unlike `PayGroup.clean()`, the age check **refuses** when the figure is not loaded
(D-101): there is no staleness guard behind it, and the failure is an offence under
s43(3) rather than a wrong number on a payslip.

**Rate derivation has exactly one statutory constant** — BCEA s35's four and one-third,
in `statutory_parameter` and handed into `employees/rates.py` rather than known by it
(D-104). **Weekly is the hub**: every basis converts to weekly first and hourly and daily
both come off it, because deriving daily as hourly × `hours_per_day` disagrees with
weekly ÷ `days_per_week` whenever the two do not reconcile — and the employee would be
paid one figure for a day of leave and another for a day of work (D-106). The three
derived rates are stored columns, not properties: recomputing on read would recompute
with today's hours and today's factor, and a 2029 re-run of March 2026 would differ from
the payslip the employee was handed.

The minimum wage is checked on the **derived hourly rate**, raises with both figures, and
can be accepted by a named user whose id is stored (D-108).

**A statutory threshold that only answers a yes/no is stored as a declared boolean, not
as a figure** (D-110). `work_schedule.works_over_27_hours_week` is the SD7 band selector
and `employee_tax_profile.is_uif_exempt` carries its reason — the employer states which
side of the line the person is on, the statute keeps the number, and nothing is
hard-coded or needs a citation nobody can stand behind. When a statute's number only ever
decides something a human already knows the answer to, capture the answer. The pay cache is refreshed as
at **today**, never as at the new row's effective date — that mistake moves a July rate
onto the employee list in March, and it is the bug D-18's nightly job exists to catch
(D-107).

`leave_type` is built here rather than in P6, because `employee_leave_entitlement` points
at it and an employer granting twenty-one days instead of fifteen has to say *which* leave
that is (D-127). It is the **second shared table**, and it gains an `is_system` column the
workbook does not have — `lock_system_rows()` keys on it, and without it the first employer
to tidy their leave list takes `ANNUAL` out of everybody's, because a DELETE is checked
against the policy's USING clause only (D-93). The table ships **empty**: seeding the two
annual variants and the four sick-leave evidence types is a compliance reading and belongs
with the P6 engine that has to honour it.

**`employee_recurring_component` matches sheet 02 exactly — `balance_outstanding` and
`total_deduction_cap_pct` are both in it** (O-17, resolved by D-135). A parallel session on
14 September removed both, reaching the opposite conclusion for reasons good enough to
record (see O-17), but the workbook wins until Kobus says otherwise, and here it says keep
them: the loan balance is a cache the payroll engine has no ledger to derive from until P7,
and the per-component cap is a per-employee override the gazette permits rather than a
duplicate of one.

**What the table does NOT carry is an overlap exclusion** (D-135). It shipped with one —
two live rows for the same component were forbidden — and sheet 03 has no such constraint
here. Two advances of the same component running at once is ordinary; forbidding it made an
employer invent a second component so the deduction could coexist, and it then stopped being
recognisable as an advance in the BCEA s34 total, a compliance failure no test caught. The
guard that survives is a `UniqueConstraint` over `(employee, payroll_component,
effective_from, amount, percentage_of_basic)` with `nulls_distinct=False` — amount and
percentage_of_basic are mutually exclusive and therefore nullable, and NULL = NULL is unknown
in PostgreSQL — which catches the case the EXCLUDE was really standing in for: the same line
captured twice.

**`employee_leave_entitlement` carries an EXCLUDE over `[effective_from, effective_to)` per
`(employee, leave_type)`** (D-136). It shipped in `employees/0005`, ahead of a citation for
it — sheet 03 is silent on this table but asks for exactly this shape on `employee_position`
and `work_schedule`, which is the deviation's justification. The unique on `effective_from`
alone stops two grants beginning on one day and does nothing about a grant back-dated into an
open period — the D-103 trap again — which leaves two entitlements in force for one leave
type with nothing but row order deciding between them.

**The as-at columns move on a date, not on capture** (D-132). `is_current`,
`status`, `is_billable` and the two pay-cache columns are all recomputed by
`employees/currentstate.py` — on write, and by `manage.py refreshemployeecache`
nightly. `terminate()` used to close the engagement, mark the employee terminated
and stop the billing at the moment of capture, so an employee serving a month's
notice read as having left six weeks early: every current-employee query would skip
the person the next payroll still owes a final salary, a leave payout and an IRP5.
D-107 was this bug pointing the other way. Two guards moved with it — "already
engaged" in `engage()` and "no open engagement" in `capture()` now read
`termination_date IS NULL`, because `is_current` stopped meaning "not terminated"
the day this changed.

```powershell
python manage.py refreshemployeecache                  # today, every tenant
python manage.py refreshemployeecache --as-at 2026-07-01
python manage.py refreshemployeecache --tenant 12 --dry-run
```

**The employee list is `employees/listing.py`** — grouped by pay group, ordered by
the employer's `EMPLOYEE_LIST_SORT` (D-16), headings suppressed under
`GROUP_HEADER_MINIMUM` (D-19), and a user's own re-sort remembered in
`tenant_membership.ui_preferences` (D-133), a JSONB bag whose keys and values are
checked against a registry. The employer's setting still governs the attendance grid
and the payroll run; one person's view choice is one person's. Ordering is the
ICU-collated generated columns and nothing else: *Böhmer* files between
*Bezuidenhout* and *Botha* under `en-ZA-x-icu` and after both under a byte
comparison, which is a customer saying the list looks random.

**`leave_type` had the system-row lock and not the column it keys on** (D-134). The
trigger's first statement is `IF NOT OLD.is_system`, the column was never added, and
every UPDATE and DELETE on every row of the table failed inside it — an employer
could add a leave type and then never rename it. A green suite hid it because the
one test covering the area asserted `DatabaseError`, and `record "old" has no field
"is_system"` is one. **Assert on the message, not only the exception class**, and
the same goes for every `IntegrityError` test in this codebase.

**`document_category` is the THIRD shared table** (D-138), same shape as
`payroll_component` (D-87) and `leave_type` (D-127). It carries `is_system` from its
first migration rather than retrofitting it the way `leave_type` had to — sheet 02
gives the table no such column, and D-134 is exactly why one is needed anyway:
`lock_system_rows()` keys on it, and installing that trigger on a table without the
column fails every UPDATE and DELETE on the table, a tenant renaming its own category
included. `documents/tests/test_document_category.py` proves a tenant can rename and
delete its own row the moment the table exists, rather than after a second migration.

**`document`'s exclusive arc is implemented VERBATIM from sheet 03, and it is
asymmetric on purpose** (D-139). An employee- or workplace-attached document may also
name the employer — the last two arms of the CHECK deliberately leave `employer_id`
free rather than forcing it null, because an employee belongs to an employer and a
workplace belongs to an employer. Four real foreign keys rather than a generic
`owner_type`/`owner_id` pair, because a generic pair produces orphans the first time a
referenced row is deleted and nothing in the schema stops it (D-41). No EXCLUDE here:
two documents about the same thing coexisting is normal — a renewal chain
(`supersedes_document`, cycle-checked in `clean()` with a depth cap) is the model for
that, not an overlap the schema should forbid.

**The seeded "tenant/employer" categories attach to the employer, not the tenant**
(D-140), and this is flagged rather than asserted as the workbook's own answer. Sheet
03 groups CIPC registration, VAT registration and the rest under one label,
"tenant/employer", but `applies_to` holds exactly one value and there is no combined
arm to hold it. `employers.Employer` already carries `registration_number` and
`income_tax_reference` — this schema's CIPC number and tax reference already live
there — and a tenant can run several employing entities, each needing its own
registration documents. `documents/categories.py` is the seed list; if this reading is
wrong the fix is a data update to the seeded rows, not a schema change.

**`is_confidential_by_default` is TRUE for seven seeded categories** (D-143): ID copy,
passport, work permit, asylum permit, police clearance, the employee's own bank
confirmation and the next-of-kin form. Unlike the expiry and sector questions above,
this one is not the workbook staying silent on something it should decide — it is a
question this codebase can answer on its own facts about what a category already
seeded happens to contain: police clearance is POPIA s26 special personal information,
the identity documents carry the ID number D-77 already encrypts, the bank
confirmation is D-111's ghost-employee fraud surface (the employee's copy, not the
employer's own banking letter), and next-of-kin exposes a third party who never dealt
with the employer. Because seeding never updates an existing row, an environment that
had already run `seeddocumentcategories` needed a real fix rather than a note — see
`documents/0002_backfill_confidential_categories`, which travels with `migrate` rather
than needing a second manual step. **The `tenant` arm of `applies_to` stays
deliberately unseeded** — a tenant-level document is the subscriber's own (the service
agreement, the POPIA operator agreement, the debit order mandate), while every
registration document belongs to the employing entity (D-140).

**`employee_import_batch` closes P4, and it is NOT in the workbook** (D-144). Sheet 02
has only `attendance_import_batch` (P5); this table is modelled on it, minus
`period_start`/`period_end` — a one-off employee upload has no period the way a month
of attendance does. An ordinary `TenantScopedModel` table with the plain `enable_rls`;
no shared rows, no system-row lock, nothing beyond what every employer- and
employee-data table already carries.

**`employees/importing.py`'s central rule: preview IS apply, rolled back** (D-145).
Both run the identical per-row code — the same `Employee` creation with its identity
checks, the same `engage()`, the same `capture()` — each inside its own savepoint, and
differ only in whether the outer transaction commits. There is no `validate_only` flag
anywhere in the chain: a second validation path by another name is exactly what D-122
exists to prevent, since a shortcut that exists only for previewing is a shortcut
somebody eventually reaches for apply too. Rolling back a preview burns primary key
sequence values — that is a fact about sequences, not a defect. Apply refuses outright,
writing nothing, while any row carries a blocking error; `accepted_count` and
`rejected_count` describe what was found, not a licence to write half a batch. Legal
status transitions are enforced in `transition()`, not only by the CHECK on the column
— the CHECK proves the value is a known one, never that the move from the row's
previous value was legal.

**A below-minimum-wage row is a warning, batch-wide, exactly as D-108 already treats
one row** (D-146). `apply_batch()` refuses — nothing written — when any row is below
the floor and no `acknowledged_by` was named; applying with one stores that user's id
on every affected `employee_remuneration` row, the same column the single-capture path
already uses. No default, no batch-level bypass: an importer that silently dropped
those rows would be indistinguishable on the surface from one that captured them
correctly, and the gap would only surface when someone reconciles pay months later.

**The uploaded spreadsheet's content is purged once the batch reaches applied or
reversed** (D-141), through `core/files.py`'s `purge_content()` — `file_object`'s row
survives as the trail, `content_purged_at` records that the bytes are gone.
`employee.id_number` is encrypted precisely because a spreadsheet holds it in the
clear; a source file left sitting in storage after the import completes puts that
protection right back. `employee.created_by_import_batch` (D-142) is the one column
answering both what a reversal must remove and where an employee came from,
permanently — every child row already cascades from the employee, so the batch's
footprint is exactly its employees.

**The template is generated from one column spec, read by both sides** (D-147).
`EXPECTED_COLUMNS` in `employees/importing.py` is what `manage.py
generateemployeeimporttemplate` writes and what `parse_workbook()` reads — D-122's own
reasoning restated for this table: a hand-maintained sheet that drifts from what the
importer expects produces failures the employer cannot diagnose, and one spec cannot
drift from itself. The one number in the generated file — the example row's rate — is
a plain `int`, not a `Decimal` literal: it is a plausible salary for a demo cell, not a
statutory figure, and `test_no_hardcoded_rates` scans this file exactly like every
other module in `employees/`.

**P5 — Attendance & Time: chunk 1 of three** (14 September 2026). 882 tests green.
`attendance_day` exists, tenant-scoped, and `calculators/attendance.py` is **the first
real calculator** — the calculators rule stops being aspirational from here. Pure
functions only: no ORM import, no database access, no file I/O, no `datetime.now()`.
Every multiplier, cap, window and minimum it uses is a field the caller reads from
`working_time_rule_set` through `statutory.resolve`; none turned out to be missing.

**Two forward references, both following `core.TenantMembership`'s own house pattern**
ahead of P4: `leave_application_id_ref` and `locked_by_payroll_run_id_ref` are nullable
`BigIntegerField`s, named for what they become in P6 and P7. Sheet 03's CHECK —
`(day_type='leave') = (leave_application_id IS NOT NULL)` — is kept against the
placeholder exactly, which makes a leave day impossible to create until P6 exists. That
is honest rather than an oversight: this system cannot yet say what leave was taken.
`import_batch_id` is left out entirely rather than shipped as a third placeholder —
chunk 3 is a week away, and a column nothing populates is not a forward reference.

**Locking is a trigger, not an application check** (invariant 4), and it is a THIRD
shape of frozen row after `append_only()` and `lock_system_rows()` — see
`core/db/rls.py`'s `no_update_when_locked()` (D-151). Unlike a ledger, DELETE stays
legal: reversing a whole finalised payroll run removes the lock along with everything
else. `attendance/capture.py` checks first and names the payroll run; the trigger is
the backstop for every path that does not come through it.

**Three hour-bucketing shapes are modelling choices, not derivations, and neither is
settled by a published worked example** (D-148): a Sunday splits into ordinary-Sunday-
plus-overtime only when Sunday is an ordinary working day for that employee, and
otherwise every hour is Sunday hours with no overtime split at all; a public holiday
worked never splits into overtime, unlike an ordinary day; and a standby day is its own
path that replaces rather than adds to the day's ordinary/Sunday/public-holiday
bucketing. All three are flagged for the labour law review (O-06). One consequence is
flagged separately (O-19): an employee who works an ordinary shift and then goes onto
standby the same calendar day has no `attendance_day` row that can represent both, and
nothing in this chunk builds around that pattern.

**`days_worked_equivalent` is D-149, and it is a choice too.** 1.000 for
`leave`/`absent_paid`, 0.000 for `absent_unpaid`, and for every day with hours in it the
ratio of hours actually paid — worked hours plus the SD1 guarantee — to the employee's
own scheduled hours for that day, capped at 1.000. An equally defensible rule would use
the rule set's statutory per-day cap as the denominator instead, or count only
`ordinary_hours`; either would move the number on a short or non-standard day. Flagged
for O-06 rather than left implicit in the code.

**There is no golden-file test for hour bucketing, and that is permanent, not
pending** (D-150). Unlike PAYE or the minimum wage, no regulator publishes a worked
hour-bucketing example to reproduce. Fabricating one from this codebase's own
arithmetic and calling it golden would prove nothing — it could only ever agree with
itself. Covered instead by table-driven tests for every boundary the task named
explicitly, and Hypothesis property tests for the invariants: no bucket negative, the
worked total never exceeds 24 hours, ordinary never exceeds the schedule, overtime is
exactly the excess, a day with no work produces no paid hours but the guarantee. 100%
branch coverage — `[tool.coverage.run] branch = true` in `pyproject.toml`, added this
chunk: without it `--cov-fail-under=100` was only ever measuring line coverage, which
an if/elif chain can satisfy while still skipping one of its branches entirely. CI's
calculator coverage step is a self-removing guard keyed on a real `.py` file existing
under `calculators/` — it now runs for real, with no change to the workflow file
needed.

**Buckets are computed at capture and stored, never recomputed on read** (invariant 2)
— `attendance/capture.py` reads the rule set in force **on the work date**, not today's.
A March day re-read in 2029 must show what March produced.

**Still open in P5:** the capture screen itself (a monthly grid), pre-fill from
schedule, bulk actions, the live exception panel's UI (the pure calculation behind it,
`evaluate_exceptions`, exists), `timesheet_summary`, and `attendance_import_batch`
(chunk 3). Consecutive sick days needing a leave application are P6 and are not stubbed.

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
- Contract cleaning **Area B (KwaZulu-Natal)** has no rate: the gazette states none and points
  at the BCCCI collective agreement, which nobody has. A KwaZulu-Natal contract cleaning
  employer cannot be onboarded until it is loaded. (Area B, not Area C — D-61 had the
  lettering the wrong way round, and D-118 corrected it)
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
