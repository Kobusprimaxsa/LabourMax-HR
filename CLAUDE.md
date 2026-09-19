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

**PROVE EVERY GUARD FAILS.** A constraint, trigger, policy or coverage gate is not
in force until you have watched it reject the case it exists for. This project has
now shipped five guards that read correctly and did nothing at runtime — the P0
five, `leave_type`'s system-row lock keying on a column that did not exist (D-134),
`dbcheck` demanding extensions nothing uses (D-137), and `--cov-fail-under=100`
measuring only line coverage until `branch = true` was set (P5 chunk 1). Every one
was found by accident, not by design. Writing the guard is not the work; writing
the violating case, watching it fail with the right message, and keeping that
failure as a test is the work. This is why every test file in this codebase that
exercises a refusal asserts on the message and not only the exception class (D-134
again) — a guard that raises the wrong error for the right reason is exactly as
silent as one that does not raise at all.

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

**The line that actually triggers this is the one that looks like nothing at all.**
`row.employee` reads as plain attribute access on a row you are already holding — it
is a database query. A lazy foreign key not yet loaded hits the manager and the policy
exactly like any other query, and with no tenant pinned it returns nothing, on a row
in your hand. `update_or_create()` has the equivalent trap on the write side: the
manager does not inject `tenant` into it, so it needs `tenant=` passed explicitly in
the lookup or defaults, or it looks for, and fails to find, a row that is right there.
Three bugs of exactly this shape were found in one P5 chunk, and all three were found
by writing the failing case first, not by reading the code — the line reads as
harmless attribute access, so review does not catch it; a test that pins no context
and asserts on the result does.

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

**EVERY CONSTRAINT OVER A NULLABLE COLUMN IS PERMISSIVE BY DEFAULT.** This is one PostgreSQL
behaviour, not three unrelated ones, and it has now been found in three shapes:

- A `UniqueConstraint` over nullable scope columns permits DUPLICATES, precisely for the rows
  where every scope column is NULL — which in `minimum_wage_rate` is the general National
  Minimum Wage, the row most likely to be loaded twice. `NULL = NULL` is unknown, so two NULLs
  are never equal and the constraint never sees a clash. Fixed with `nulls_distinct=False`.
- An `ExclusionConstraint` over a nullable scope expression permits OVERLAPS for the same
  reason — `employee_recurring_component` needed `Coalesce(col, 0)` inside the exclusion
  expression so two NULL-scoped rows compare equal instead of each being unique.
- A `CHECK` that evaluates to NULL rather than FALSE counts as SATISFIED — PostgreSQL only
  rejects a row when the expression is FALSE, never when it is merely unknown. A per-branch
  test over a nullable column (`Q(transaction_type="accrual", hours__gt=0)` when `hours` IS
  NULL) evaluates NULL, and ORed against another branch's plain FALSE the whole CHECK stays
  NULL — which PASSES, silently, for exactly the wrong-signed row the CHECK exists to catch
  (`leave_transaction_sign_matches_type`, D-170). The fix is the same shape as the other two:
  make NULL's meaning explicit, here with `__isnull=False` guarding every branch that touches
  the nullable column, so a NULL resolves the branch to a definite FALSE instead.

**If a constraint touches a nullable column, its author must say what NULL means** —
`Coalesce`, `nulls_distinct=False`, or an explicit `IS NULL`/`IS NOT NULL` on every branch —
or the constraint silently does not apply to the rows carrying NULL, which are usually the
exact rows it was written for.

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

**The contract is `calculators/base.py`, and `calculators/tests/test_contract.py` enforces
it** (D-207). One frozen input structure in, one frozen result out. Every statutory figure
arrives as a `StatutoryFigure` carrying the TABLE and PRIMARY KEY of the row it came from — the
key, not the citation, because a citation gets corrected and the key still opens the row a 2026
payslip was computed against. Every monetary output is a `Money` carrying `.exact` (six places)
and `.rounded` (two, ROUND_HALF_UP); only the payslip line reads `.rounded`. The import guard
reads each module's source and refuses anything outside decimal / dataclasses / datetime /
typing / enum / collections / calculators, and refuses a clock call however it is spelled.
Provenance is a PROTOCOL, `base.Sourced` (D-215): a PAYE bracket is four numbers off one row,
so it carries the key once rather than repeating it on four `StatutoryFigure`s.

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

**Re-encoded a fixture? It needs a new version label, not a hand-deleted row** (D-199, O-21
closed). Regenerating a fixture changes its checksum and `checkstatutory` then refuses it by name.
Give the file a new label (`REF-2026.03.01-RULES-r2`) and run `loadstatutory <file> --supersede
REF-2026.03.01-RULES --reason "..."`: the old version row is kept and marked superseded, only
citations and notes may differ, and a changed FIGURE is refused by name — that is a new gazette and
gets an ordinary load. Never delete a `reference_data_version` row.

**A clone of this repo has empty reference tables.** The fixtures in `reference/` must be
loaded before anything works, and `checkstatutory` refuses rather than reporting success on an
empty database (D-74) — every check iterates rows, so over no rows they all pass.

```powershell
python manage.py loadstatutory --all
python manage.py seedcomponents
```

**Verification is a person with a gazette, and this is the shape of that job.**
`exportverification` writes every loaded figure to a workbook grouped by SOURCE
DOCUMENT, so each gazette is opened once; `importverification` reads the ticks back,
records each one in `reference_figure_check`, and calls `verifystatutory` for every
version the DATABASE now says is fully checked. **The workbook is disposable and the
database holds the record** (D-256): export as often as you like, each one pre-fills
what is already done and leaves newly loaded figures blank. A value edited in the
spreadsheet REFUSES its version and names the row, because an importer that could
write a figure is every loader guard routed around by a spreadsheet (D-251).

```powershell
python manage.py exportverification --verifier you@example.com   # reference/verification/
python manage.py importverification <file.xlsx> --current-through 2027-02-28 --golden-tests-passed
python manage.py importverification <file.xlsx> --current-through 2027-02-28 --dry-run
# import before re-exporting over a file: --force only ever loses ticks never imported
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
  slightly. A small variance against another package is not automatically a bug. This build
  uses the **statutory rates**; every SARS worked example uses the **deduction tables**, and
  the gap runs about R30 a year on a small salary and R124 at R336 000 (D-212).
- **BCEA earnings threshold**: a high earner loses only s18(3), **not all of s18** — they keep
  the public holiday pay entitlement.
- **Commission is excluded** from the UIF contribution base. Bonuses are not.
- **Bonuses in PAYE**: annualise regular pay ×12, then add the annual payment **once**.
  Multiplying a bonus by twelve is the classic December over-deduction.
- **Parental leave has TWO totals, and the condition between them is not ours to see.** Under
  the interim reading-in in *Van Wyk v Minister of Employment and Labour* [2025] ZACC 20,
  3 October 2025: a single parent, or **the only employed party**, gets four consecutive months
  (read-in s25(1)); where **both parties are employed** the parties get four months and ten days
  **in the aggregate** (read-in s25(4A)), divided as they agree. Whether the other parent is
  employed is a fact about someone who is not this employer's employee, so the shape and the
  share are **declared and captured, never computed** (D-201, D-202). s25A is deleted: there is
  no standalone ten-day parental leave. Adoption and commissioning leave are four months too,
  not the pre-judgment ten weeks. MATERNITY and ADOPTION are sub-types of PARENTAL and resolve
  to ONE balance. Stored as months plus days, never as a day count, and the month-end clamps
  (D-204). The suspension ends 3 October 2028: the quantum then REFUSES rather than falling
  back, while the under-two adoption limit FALLS AWAY, both as loaded data (D-203).
- **The maternity restriction runs after the birth, not before it.** A birth mother may not
  work for six weeks after giving birth unless certified fit; separately, she may start leave
  up to four weeks before. Two different rules, two columns.
- **Leave pay, notice pay and severance all use the same s35(5) remuneration** — s35(5) says
  so itself, which is why the rule lives in `calculators/remuneration.py` and not in whichever
  calculator needed it first (D-226). The
  13-week average under s35(4) applies to all three. Whether it applies at all is a DECLARED
  boolean, because "fluctuates significantly" has no gazetted threshold (D-220). The average is
  never floored at the contractual rate (D-221).
- **A premium's multiplier may not be per HOUR — read the section.** s10(2) and s16(1) are per
  hour; **s18(2)(b) is per DAY** ("double the amount referred to in paragraph (a)", and (a) is the
  wage for that day), and s16(2) floors a short Sunday at the ordinary daily wage. The columns are
  all called `*_multiplier` and sit in a row together, which is exactly how the public holiday one
  gets priced per hour and underpays every short holiday shift (D-217).
- **An absent statutory figure is a boolean, never a zero.**
  `working_time_rule_set.accommodation_deduction_capped` (NOT NULL, **no database default**) plus a
  nullable `accommodation_deduction_max_pct`, two CHECKs holding them together, and
  `statutory.resolve.accommodation_cap()` answering `NOT_LOADED` / `NO_CAP` / `CAPPED(pct)` (D-198
  amended). 0.00 used to mean "this instrument states no cap", which made a real zero cap read as
  UNLIMITED and — the one that would have bitten — made any new rule set row whose author never
  thought about the column silently uncapped. No default, so such a row fails instead. The other
  zero-means-no-figure columns (`night_allowance_value`, the three standby columns) still carry the
  old encoding; the same reasoning applies to them when one is next touched. **The night
  allowance is no longer one of them** (D-218, O-22's night half): `night_allowance_type` is a
  `TextChoices` with a CHECK and `night_allowance_value` is NULL where the instrument states no
  amount, which is the BCEA's and SD7's position under s17(2)(a). `calculators/gross.py` refuses
  to price a **standby** day at all until the three standby columns get the same treatment.
- **The s22(3) sick ratio is not a second entitlement.** One per 26 days worked restricts how
  much of the ONE s22(2) cycle entitlement is available in the first six months. So the first
  cycle resolves to E − A by default, and s22(4)'s "may" is the employer's election
  (`SICK_FIRST_CYCLE_REDUCTION`, D-181 amended, D-186).
- **Unpaid leave stays in the application's own unit.** `unpaid_days` for a days-basis
  application, `unpaid_hours` for an hours-basis one, the other zero (D-188). Converting days
  to hours at capture is the D-106 conversion one step earlier, and D-164 forbids it.
- **Family responsibility eligibility is two limbs, in all three instruments**: longer than
  four months AND at least four days a week — BCEA s27(1), SD1 cl 22(1), SD7 cl 21(1)
  (D-189, D-194). SD7 departs on the quantum only.
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
| Employer settings | Registered in `employers/onboarding.py`, read through `setting_value()`, which pins the employer's tenant itself. `SICK_FIRST_CYCLE_REDUCTION` defaults TRUE because the single-entitlement reading of s22 produces E − A and almost every employer wants it (D-186). `UNAUTHORISED_ABSENCE_TREATMENT` defaults `unpaid` (unpaid, no leave spent; `annual_leave` = charged AND paid, never both unpaid and charged, D-195). A setting with `choices` refuses a stored value outside them. `ACCOM_DED` is a percentage of the wage, never a rand amount (D-197), refused above the gazetted ceiling (D-198). Never read a setting inside `calculators/` — pass the resolved value in |

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
calculators/    pure calculation functions — no ORM, no I/O (the contract: base.py)
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
- **Check a test count against git, per file, never against a doc.** Collect in a worktree
  at the old commit and diff `file: count` lists. Orphaned bytecode is how lost tests hide:
  20 from a parallel session survived only as a `.pyc` (D-191). pytest names them
  `name.cpython-314-pytest-X.Y.Z.pyc`, so strip from `.cpython-` or every test reads as orphaned.

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

~~`SEVERANCE` ships **inactive**~~ **CLOSED** (D-90, closed by D-114, restated D-253).
It shipped inactive because 3901 was not loaded and pointing it at 3601 would be wrong on
the IRP5 and wrong on the rate. 3901 and 3907 are loaded from SARS PAYE-AE-06-G06 revision
13 with their base flags copied from the guide, and `seedcomponents` now seeds SEVERANCE
**active** against 3901. What it still needs is P7 chunk 8 assembly: the s41 amount comes
from `calculators/termination.py`, the tax comes from `calculators/paye.py`'s directive
path, and nothing yet joins them on a payslip line.

```powershell
python manage.py seedcomponents          # after loadstatutory --all; idempotent
python manage.py seedcomponents --list   # the catalogue, without touching the database
```

~~Still open in P3: the municipality-to-area data.~~ **CLOSED 20 September 2026
(D-255)** — all twelve municipalities GN R.7083 itself names are loaded against Area A,
and under D-118 the table stops there: Area B is a province and Area C is a residual, so
neither is a list and neither has rows. Nothing is missing, and the table must not become
a gazetteer.

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

**Rate derivation's statutory constant is RESOLVED, not global** — there are two, and which
applies is a fact about the instrument governing the employee (D-236). BCEA s35(3)'s four and
one-third is the unscoped row; the BCCCI Main Agreement's clause 3 makes it **4,33** for
KwaZulu-Natal contract cleaning, loaded scoped to that sector and area. `statutory_parameter`
carries nullable `sector`/`sector_area` and `resolve.parameter()` narrows most-specific-first,
so every unscoped caller lands where it always did. **D-104 still holds in the part that
matters**: `employees/rates.py` knows no figure at all — it is handed one, now looked up on the
employer's sector and the workplace's area (D-239). **Weekly is the hub**: every basis converts to weekly first and hourly and daily
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

**P5 — Attendance & Time: build complete, chunk 3 of three, plus a hardening pass**
(14 September 2026). 935 tests green. `attendance_day` exists, tenant-scoped, and `calculators/attendance.py` is **the first
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
`import_batch_id` was deliberately left out of chunk 1 rather than shipped as a third
placeholder a week away from being replaced; chunk 3 adds it as a real FK from day one,
alongside the table it points at.

**Locking is a trigger, not an application check** (invariant 4), and it is a THIRD
shape of frozen row after `append_only()` and `lock_system_rows()` (a FOURTH,
`no_change_when_finalised()`, arrived with the payslip in P7 chunk 6 — D-229) — see
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
for O-06 rather than left implicit in the code. **AMENDED in chunk 2: "1.000 for leave"
holds only while a leave day is whole-day.** P6's `leave_application` brings part days —
a half day of annual leave must yield 0.500, read by both daily-rate pay and leave
accrual — and P6 must revisit `FULL_DAY_EQUIVALENT_TYPES` before
`leave_application_id_ref` becomes a real FK.

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

**`timesheet_summary` is a CACHE and nothing more** (invariant 3), and chunk 2's central
rule is that recomputation always rebuilds from `attendance_day` from scratch — there is
no incremental update path, because an incrementally-maintained cache that has drifted
cannot be told apart from a correct one. `total_public_holidays_not_worked` reads the
employee's **schedule**, never the calendar directly: a high earner above the BCEA
earnings threshold loses s18(3) but **keeps** the public holiday entitlement, so this
figure is never gated on earnings, only on whether the day is one the employee
ordinarily works. `total_unpaid_days` is a plain row count and the other two day-totals
sum `days_worked_equivalent` (D-154) — `days_worked_equivalent` is *defined* as 0.000
for an unpaid day, so summing it for `total_unpaid_days` would always read zero, which
is not what that column is for.

**Staleness is answered two ways, and the two are deliberately independent** (D-153).
`attendance/staleness.py`'s signal on `AttendanceDay` `post_save`/`post_delete` sets
`is_stale` the moment a covered day changes — a signal rather than a check inside
`attendance/capture.py`, so chunk 3's import batch and any future writer are covered by
construction, with no call site to remember. `attendance/summary.py`'s
`is_actually_stale()` is the ground truth and does not look at the flag at all: it
compares `computed_at` against the covered days' own `updated_at`, so a flag that was
missed — or cleared by hand, which the test suite proves against directly — is still
detectable. **PROVE EVERY GUARD FAILS** (see the Non-negotiables) is not a slogan here:
both halves of this pair are tested by first watching them NOT fire, then watching them
fire.

**The capture grid (`attendance/grid.py`) and approval (`attendance/approval.py`) are
services, not screens** — no views, no templates, the same as everything else in this
codebase so far. Pre-fill is for salaried bases only (D-25, already settled via
`PayGroup.is_attendance_driven`): hourly and daily open blank, because a pre-filled
hourly day nobody looked at is an invented wage. A pre-fill proposal is never a saved
row. Bulk fill goes through `attendance.capture.capture()` for every date and skips
anything already captured — never a second, faster write path. Approval refuses
outright, naming each one, while any BLOCKING exception from chunk 1's
`evaluate_exceptions` stands over the span; a warning does not block, and a locked day
is refused by the trigger itself rather than a Python-side copy of the same check.

**`attendance/completeness.py` is the P7 hook, and getting its direction backwards
breaks payroll two different ways.** `missing_attendance_days()` answers "what is
missing" only for attendance-driven bases (hourly, daily) — for a salaried employee, no
row means an ordinary day worked, so the function returns nothing rather than flagging
every uncaptured day of a month nobody was ever going to capture one by one. P7's
validation gate will call this; it is not built here.

**Chunk 3 closes the phase's build: `attendance_import_batch`, and a reverse that
restores rather than only deletes** (D-155, D-156). Unlike the employee import, which
only ever creates, re-importing a corrected attendance file is the ORDINARY case — it
routinely REPLACES a day that already exists. A LOCKED day is refused by name in the
row loop before `capture()` is ever called, so preview reports it rather than a trigger
dying at apply time; an APPROVED day is refused the same way unless the caller passes
`allow_replacing_approved=True`, because approval is a human judgement call an import
must not silently override; a CAPTURED day is freely replaced, and the preview counts
REPLACED separately from CREATED. Reverse restores every replaced day to its exact
prior values — a new `prior_state` JSONB column on the batch, not in sheet 02, snapshots
each replaced day before `capture()` overwrites it — and deletes only the days the batch
created outright. **The importer supplies raw capture only**: the template carries none
of `ordinary_hours`/`overtime_hours`/`sunday_hours`/`public_holiday_hours`/`night_hours`,
proven by a test that asserts on `EXPECTED_COLUMNS` itself. It also, deliberately,
carries no `standby_hours_worked` column despite the brief that specified it naming
one — chunk 1's calculator computes that figure from the same worked-hours span every
other bucket comes from, and there is no input slot for a caller to hand it in directly;
adding one only for the importer to write straight into a bucket would be exactly the
drift the "raw capture only" rule exists to stop. A bare `hours_worked` total (the
alternative to time_in/time_out) is a small, genuine extension to
`calculators/attendance.py`'s `AttendanceDayInput` and to `capture()` itself, not an
importer-only shortcut — a day captured this way honestly reads zero night hours, since
there is no time span to test against the night window.

**The shared bulk-import mechanics now live in `core/importing.py`** (D-155), used by
both this importer and the employee import (D-122). The extraction held cleanly rather
than fighting the two shapes: the status machine, the savepoint-based preview/apply/
reverse skeleton, the column-spec and report dataclasses, the template builder and the
source-file purge are identical between the two; what differs — what a row means, and
what "apply" and "reverse" actually write — stays in each app, reached through two
small hooks (`extra_refusal` for a second whole-batch refusal reason, `on_success` for
extra fields a domain needs saved once a batch commits) rather than forcing the
difference into a single function. The employee import's twelve tests pass unmodified
against the extracted code, which is the evidence, not just the intent.

**A bare `hours_worked` capture genuinely loses `night_hours`, and now says so — but it
does NOT lose `standby_hours_worked`, which the brief that raised this assumed** (D-157,
a post-chunk hardening pass). `_night_hours()` needs an actual time span to test against
the night window, and returns zero unconditionally without one — a real gap, closed with
a WARNING rather than a refusal: a standby day captured as bare hours always warns
(standby is not part of the ordinary schedule, so there is nothing to test); an ordinary
day warns only when the employee's own schedule for that weekday has both a start and
end time on file and that span crosses the night window, silent otherwise so the
household employer whose worker does eight daytime hours never sees it. Standby pay
itself needed no fix: `bucket_day()`'s `is_standby` branch computes `standby_hours_worked`
from the worked total and the flag alone, never from clock time — proven by reading the
code rather than assumed, and the outright refusal the brief asked for on every bare-hours
standby day would have been inventing a restriction with nothing behind it. One related,
pre-existing, and separate fact surfaced along the way: `RuleFigures.standby_window_start`/
`standby_window_end` are loaded and never consulted by any bucketing logic at all —
flagged as O-20 for someone to confirm is deliberate before P7 prices standby pay, not
fixed here because it predates this pass and nothing asked for it. `capture()`'s returned
row now carries a non-persisted `capture_warnings` attribute, surfaced identically whether
the day was written through a single manual call (the grid's own path) or through the
bulk importer, because both are the same function and neither duplicates its logic.

**On P5's own definition of done — read literally, it does not yet pass, and that is
stated here rather than ticked.** "A month for twenty employees is captured in under ten
minutes" needs the capture screen this phase never built; every capture path
(`attendance/capture.py`, the grid, the importer) is a service with no UI in front of it,
so the ten-minute claim has nothing to be measured against yet. "An uncaptured
attendance-driven day blocks the payroll run" needs P7's validation gate reading
`attendance/completeness.py::missing_attendance_days()` — that function exists and is
correct for exactly this purpose, but nothing calls it yet, because there is no payroll
run to block. What chunk 3 closes is real: every table and service the phase specified
is built, tested and RLS-isolated, including the one the workbook didn't carry
(`prior_state`) and the one built ahead of schedule in chunk 1
(`calculators/attendance.py`, with its own coverage gate). Both halves of the phase's
own success criterion are demonstrable only once P5's screens and P7's gate exist — this
is a prerequisite completed, not the outcome itself.

**Still open in P5:** the screens themselves (the capture grid, the exception panel) and
`timesheet_summary`'s own display — UI work this codebase has not started. Consecutive
sick days needing a leave application are P6 and are not stubbed.

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

**`termination_notice_band` closed the notice band gap** (D-68, 14 September 2026).
`termination_rule_set`'s three `notice_weeks_*` columns could express BCEA's six-month
and one-year thresholds but not SD1's boundary at FOUR WEEKS of service, nor its unit —
clause 23(1)(a) states one WORKING DAY, worth a fifth or a sixth of a week depending on
the working week, and contract cleaning is exactly the sector that runs six-day weeks.
The replacement is a cited child table, `(service_from, service_to, notice_value)` each
with its own unit, built by `tools/build_notice_band_fixture.py` and resolved by
`statutory/resolve.py::notice_band()` — which computes nothing itself: it converts each
band's own `(value, unit)` to a calendar date via `dateutil.relativedelta`, never a
day/week/month/year count written in the module.

**Which side of an exact boundary a service length falls on is DATA, per boundary, not
a global convention** (D-158, corrected the day after it was first written). The first
version put every exact boundary in the upper band, reasoning that this matched
`EffectiveDatedModel`'s own inclusive-start/exclusive-end convention. That was wrong
against the statutes themselves, not against a hypothetical: BCEA s37(1)(a) gives one
week for "six months **or less**" and SD1 clause 23(1)(a) gives one working day "**during
the first four weeks**" — both name the boundary as the LOWER band's, in so many words,
and the upper-band reading gave a resigning employee less protection than the Act
requires. Notice is symmetric (SD1 clause 23(1)(c) forbids an employee owing more notice
than the employer does), so over-stating the period is not a safe direction the way it
looks — it holds a resigning employee in employment longer than the law permits. Caught
by three failing tests, one per sector, written before the fix. `service_from_inclusive`
and `service_to_inclusive` now sit on every band, read from each clause's own wording;
`statutory/checks.py::check_notice_bands()` reconciles that exactly one of "the lower
band's `service_to_inclusive`" and "the upper band's `service_from_inclusive`" is true at
every shared boundary — both true is an overlap, both false is a gap, and a plain
value-equality check catches neither. BCEA's one-year mark is the one boundary that is
genuinely a judgement call: s37(1)(b) "not more than one year" and s37(1)(c)(i) "one year
or more" both name it, and four weeks is the reading loaded, flagged for the labour law
review rather than asserted as settled (O-06). SD1's own pay-in-lieu-of-notice
formula is read as of P7 chunk 5, and the note that used to stand here was wrong about
the clause (D-224): 23(1)(d) gives figures for one working day and for **four weeks'
notice** — the figure being "double the weekly wage" — not "for two weeks", so there was
never a missing band. Its amounts are a floor ("not less than") and BCEA s38 pays more in
every case, so the two do not conflict.

**The fixture checksum guards a reload, not the time in between** (D-161). The loader
fingerprints a fixture and refuses to reload a version label under different content —
but that comparison only ever fires as a side effect of attempting a reload.
`statutory/checks.py::check_fixture_checksums()`, run by `checkstatutory`, makes it a
standing fact instead: every loaded version's checksum against its fixture file as that
file reads right now, so an in-place edit is caught even if nobody ever tries to reload
it. Closing D-158 also surfaced a real question this codebase has not answered: a
structural fix to a cited table (as D-68 and D-158 both were) currently requires deleting
`reference_data_version` rows by hand to make room for the edited fixture, which is fine
on a dev database where nothing is verified and impossible once a version's rows are the
audit record of what a real payslip was computed against. Recorded as O-21, before P11 and
before any deployment — the fixture files currently serve as both build input and audit
record, and those two roles are what collide.

**P2 — the BCCCI Main Agreement, chunk B: Area B's rule set** (19 September 2026,
D-240 to D-246). 1,617 tests green. A rule set is now scoped to a sector **and an
area**, because the agreement binds contract cleaning in KwaZulu-Natal only while SD1
still governs Areas A and C of the same sector — one sector, two instruments, which is
a scope and not a second sector (D-240). Three things came out of it that outlive the
load.

**Clause 21.1(b) cannot be resolved, and the system says so instead of guessing**
(D-241). It gives two answers between four weeks and six months: two weeks after the
first four weeks, and one week while on probation, which clause 3 caps at four months —
so both reach the same employee, and clause 21.2(a) prices only the one working day and
the two weeks, giving no payment in lieu for the one-week probation notice. A notice
band may therefore be CONTESTED: `notice_value` nullable, paired with `is_contested` and
a mandatory `contested_reason`, two CHECKs holding them together, and
`resolve.notice_band()` raising with both limbs quoted. **Notice is symmetric** (D-158),
so "take the longer as safer" is not available — over-stating it holds a resigning
employee longer than the law permits. This is a BLOCKER on O-06, not a confirmation.

**BCEA s49 does part of the design work.** s49(1)(d) and (e) forbid a bargaining council
agreement from reducing the s25 maternity entitlement or the ss22–24 sick leave
entitlement, so clause 13.3's twelve-week return cap and clause 10.3(a)(i)'s one-day
certificate rule are void to that extent and are not loaded — the certificate threshold
stays at 2 for Area B, and `test_a_kzn_employee_absent_two_consecutive_days_needs_no_certificate`
is named so it says why. Two benefits in the same clause ADD rather than reduce, so s49
does not reach them and they ARE loaded: clause 13.2's prenatal clinic day and clause
13.4(a)'s payment on return, the latter **stored as a divisor and never as 0,333333**,
because a third of R9 000 is R3 000 exactly (D-244).

**Clause 4.5(g) makes three of its own rules minimums, so they are employer elections**
(D-242), defaulting to the gazetted position, the same shape as
`SICK_FIRST_CYCLE_REDUCTION`. And clause 9.1(b)'s 28 days for more than ten years' service
is the first service-dependent annual leave entitlement here: four new `leave_rule_set`
columns behind a NOT NULL boolean with **no default**, D-198's own shape, which promptly
failed the leave app's test fixture on the next run because that fixture had never been
asked the question (D-243).

**P2 — the BCCCI Main Agreement, chunk C: verified against the gazette**
(19 September 2026, D-247 to D-250). 1,623 tests green. Every figure loaded from
GN R.7296 was read back off the gazette page by page, and the four BCCCI versions
are recorded verified through 28 February 2029 — the day before a fourth increase
would be due, the three gazetted rates landing 1 April 2026, 1 March 2027 and
1 March 2028. **The verifier of record is `claude-verification@labourmax.invalid`,
not a person.** The extension notice sets no expiry at all: GN R.7296 binds "with
effect from the first day of the month after the date of publication of this
Notice and shall remain in force until replaced by a subsequent agreement".

**The verification pass earned its keep three times.**

**The rule sets were in force a month early** (D-247). Every rule set and notice
band went in at 1 March 2026 because the three builder helpers shared the
sectoral-determination constant, while the wage rates — built by a different
tool — correctly carried 1 April. Clause 2(1) and the extension notice both put
it at 1 April, so for all of March a KwaZulu-Natal employee would have been given
this agreement's leave, hours, bonus and notice terms while the predecessor
agreement still governed them (O-30). The wage refusal masks part of that and not
the whole: nothing makes a leave accrual wait on a wage lookup.

**A reading this build had recorded was wrong** (D-248). D-244 said the agreement
"contradicts itself, cl 10.2(a) tracking BCEA s23(1)". It does not — 10.2(a)
states the same ONE-day threshold as 10.3(a)(i) and drops a "not" on top of it.
The s49(1)(e) conclusion stands and now rests on one ground instead of two. Two
smaller corrections travel with it: the 60-minute meal interval is BCEA s14(1),
since clause 8.4(b)'s hour is expressly limited to Health Care and Hospitality;
and clause 4.6 "will come into effect in the increase year of 2028" in its own
words, though no figure differs from 4.5 and 4.5(b)'s pro rata binds now.

**Verifying something opened the gate** (D-250). `in_force_on()` returns the
NEWEST usable version, so verifying four KwaZulu-Natal versions made one of them
the newest and stopped `payroll/validation.py` asking about REF-2026.03.01, where
PAYE, UIF and the NMW live. A June 2026 run went from refusing to passing because
a provincial wage schedule was checked. The gate now refuses while ANY applicable
non-superseded version is unverified and reads the SHORTEST `data_current_through`
across them. **P7 is still blocked, correctly** — REF-2026.03.01 is unverified and
the gate names it.

**What remains in P2:**

- Kobus verifies every figure against its source document, then `verifystatutory`. **The
  job now has a shape**: `exportverification` writes all 582 figures to a workbook grouped
  by source document — 19 of them, the largest being the SARS code guide at 114 figures and
  the BCEA at 86 — and `importverification` reads the ticks back (D-251). Nothing is
  pre-ticked: no earlier pass recorded which figures it checked, so the count is 582 and not
  a remainder
- ~~Contract cleaning Area B has no rate~~ **CLOSED 19 Sep 2026 (D-237).** The BCCCI Main
  Collective Agreement (GN R.7296, GG 54412, 27 March 2026) is loaded as
  `reference/ref-2026.04.01-bccci.json`: R32,40 from 1 April 2026, R34,02 from 1 March 2027,
  R35,72 from 1 March 2028. **A KwaZulu-Natal contract cleaning employer can now be
  onboarded.** What is still missing is the PREDECESSOR agreement covering 1–31 March 2026
  (O-30) — the resolver refuses that period by name rather than answering the National
  Minimum Wage (D-238). (Area B, not Area C — D-61 had the lettering the wrong way round,
  and D-118 corrected it)
- Account number lengths per bank (D-72, searched and still NULL — D-254). The
  BankservAfrica/PayInc EFT specification is not published, and the per-bank lengths on the
  open web contradict each other, so nothing citable was found and nothing was loaded. O-04
  inherits two items rather than the whole question: the EFT specification the payment
  partner supplies, or each bank's own published account format
- The golden tests, which are what finally allows `golden_tests_passed`

Chosen ahead of P1 because nothing in the payroll engine can be tested against a SARS worked
example until the reference tables exist, and a wrong UIF ceiling blocks a pilot employer in a
way billing does not. P1 also carries the most open decisions (O-03 rand amounts, O-04 payment
gateway), so starting it means stopping to ask.

**Statutory figures are never invented.** If a rate is needed and cannot be cited, say so and
stop. That applies to filling in a fixture as much as to writing code.

**P7 — Payroll Engine: chunk 1 built, assembly BLOCKED** (19 September 2026). The calculator
contract (D-207), `payroll_calculation_trace` (D-208), UIF (D-210) and SDL (D-209) are in.
**No payroll run can start until P2 is verified**: REF-2026.03.01 reconciles but is unverified,
so `in_force_on()` cannot see it, and periods, runs and payslips all read effective-dated rows.
Calculators are unblocked by construction — a pure function takes its figures as inputs.
UIF's base is `min(remuneration, ceiling)` (s6(2) excludes only the excess, so AT the ceiling
contributes in full); SDL liability is a declared boolean because s4(b) is forward-looking, and
the R500 000 threshold is an input to the SCREEN, never to the calculator.

**P7 chunk 2 — PAYE, the annual equivalent and directives** (19 September 2026, D-212 to
D-215). 1 320 tests green. `calculators/paye.py` is the statutory-rates method, and the
tables-versus-rates note above is now a settled design point rather than a warning: **every
worked example in both SARS employer guides is computed on the deduction TABLES**, which this
module does not implement and SARS itself calls interchangeable with the statutory rates
("small differences may occur … these methods are acceptable in terms of the Income Tax Act",
PAYE-GEN-01-G01 §5). So the golden file splits: everything the statutory rates settle exactly
is reproduced to the cent from published figures — six cumulative band bases, the marginal
formula, all three tax thresholds producing precisely nil, the medical credit scale, and
SARS's own annual-equivalent and pro-rata arithmetic — and the two fully worked examples are
reproduced by METHOD with both figures named and the gap held inside a bound the file labels
as a test-only sanity check.

**One formula, not four.** Paragraph 9(2)'s annual equivalent is remuneration × periods in the
year ÷ periods worked, and `periods_worked` carries 1 for an ordinary month, 7 for a mid-year
leaver, and SARS's own decimal portion 3 ÷ 7 for someone who starts on the fifth day of a
week. A bonus is added to the annual equivalent ONCE and its tax is the difference — annualise
it with the salary and the employee pays R104 528 for a year in which R55 005 was due, a
mistake that is a few hundred rand in the month itself and lands in February.

**A directive REPLACES the calculation.** No bands, no rebates, no medical credit; a fixed
percentage runs on GROSS, before the paragraph 2(4) deductions, because the guide says so in
terms. A directive with no instruction on it, and an EXPIRED directive, both raise rather than
falling back to the tables — falling back is deviating from the directive. `TaxStatus` is a
hand-copy of `EmployeeTaxProfile.TaxStatus`, and `payroll/tests/test_paye_boundary.py` asserts
the two never drift. Three readings are flagged for a tax practitioner rather than asserted
(O-23): whether a directive suppresses the medical credit, what `FOREIGN` means, and whether
`directive_amount` is per pay period.

**P7 chunk 3 — gross pay, one calculator for five pay bases** (19 September 2026, D-216 to
D-219). 1 386 tests green. `calculators/gross.py` prices the buckets
`calculators/attendance.py` produced. **BCEA ss 10, 16 and 18 each state a TOTAL for the day,
not an amount to add on top**, so there is one rule and not five: the premium line is the
section's total less what BASIC already paid for that day, and what the basic covers is a fact
about the pay basis — hourly pays the ordinary bucket (never a Sunday or holiday hour, the
buckets being disjoint), daily pays `days_worked_equivalent` (which does cover them, and cannot
cover overtime because it caps at 1.000), and a salary buys every ordinary working day. The
proof is that the same Sunday costs the same on all three.

**Two premiums were read from the Act and neither is what the column name suggests** (D-217).
**s18 prices a DAY**: s18(2)(b) is "at least double the amount referred to in paragraph (a)",
and (a) is the wage for that DAY, so four hours on a public holiday is two days' wages, not
four hours at double time. **s16(2) floors a short Sunday** at the ordinary daily wage. The
first version of this calculator priced both per hour, read as correct, and only the Act's own
text caught it — `public_holiday_worked_multiplier` sits beside three columns that genuinely
are per hour. Every section is transcribed verbatim in `calculators/tests/test_gross_golden.py`
beside the test holding the code to it, because no regulator publishes a worked premium example
(D-150's position, restated for pay).

**O-22's night half is closed, on the deadline it set** (D-218): `night_allowance_value` is
nullable, `night_allowance_type` is a `TextChoices` with a CHECK, and three constraints hold
the pair together. The generic rule-set test helper had been writing the literal `"x"` into
that column for a year, which the new CHECK caught on its first run.

**Two things REFUSE rather than guess** (D-219). A **standby day** raises, naming O-19, O-20 and
O-22 — the standby columns still carry the sentinel the night allowance just shed, and O-22
records that they cannot be settled until the standby modelling is. An employee **above the BCEA
earnings threshold** raises where the period carries overtime, Sunday or night hours: s6(3) has
the Minister determine which provisions fall away, that determination has not been read (O-24),
and s18(3) is the one exclusion already settled.

**P7 chunk 4 — leave pay and the variable-earnings average** (19 September 2026, D-220 to
D-223). 1 419 tests green. `calculators/leave_pay.py` prices what P6 counted (D-166: leave
computes days and hours, never money). **s21(1) gives two rates and s35 decides which**: the
contractual rate for an employee paid by time whose pay does not swing, and the **13-week
average** where s35(4) bites — "calculated, either wholly or in part, on a basis other than
time **or** ... fluctuates significantly from period to period". Which one applies is
**declared, not derived**: "fluctuates significantly" has no statutory threshold anywhere, so
deriving it would invent the one number the Act declines to give. The window itself is
`VARIABLE_EARNINGS_AVERAGE_WEEKS` in `statutory_parameter`, never a 13 in the module —
`test_no_hardcoded_rates` would not have caught one, a week count being an `int` (D-100's gap
again). s35(4)(b) shortens the window to the period of employment where that is shorter.

**The average is NOT the greater of the two** (D-221). s21(1)'s "at least equivalent to" sets a
floor on what must be PAID; limbs (a) and (b) say how the figure is ARRIVED AT, and (b) makes
s35 the calculation. So an average below the contractual rate is paid, with a warning naming
both figures rather than a top-up that would invent an entitlement. Two readings are flagged
rather than asserted: the divisor is the window and not the weeks actually worked, so unpaid
time inside it lowers the average (O-06); and the comparison runs at the working precision,
because comparing raw warned on every hourly employee whose rate does not divide evenly.

**What counts as remuneration is the component flag, and the calculator never re-decides it**
(D-222). s35(5) and the Minister's determination (Government Notice 691 of 23 May 2003) list
both sides; in this codebase that is `payroll_component.affects_leave_pay_average`, set per
component with its reasoning. `payroll/tests/test_leave_pay_boundary.py` pins the flagged set
by name against GN 691's limbs — the flag decides what an employee's leave is worth for the
rest of their employment and appears on no screen. **O-25 raised**: `BONUS_PRO_RATA` is flagged
FALSE, and SD1's December bonus is gazetted rather than discretionary, which GN 691(c) would
include. Not changed, because the flag is a settled decision.

**P7 chunk 5 — the termination payout** (19 September 2026, D-224 to D-227). 1 460 tests
green. `calculators/termination.py` prices s38 pay in lieu, s40(b) leave due, s40(c) the
incomplete cycle and s41 severance — and **BCEA s35 moved to `calculators/remuneration.py`**
because s35(5) names all three payments together, so one averaging rule serves s21, s38 and
s41 rather than three copies of it (D-226).

**Three findings, and two of them changed what this build believed.**

**SD1 clause 23(1)(d) was misread, and the conflict it was flagged for does not exist**
(D-224). The note that stood here said the clause "gives figures for one working day and for
two weeks, and no band in the determination is two weeks". It gives figures for one working
day and for **four weeks' notice** — the figure being "double the weekly wage". There was
never a missing band. Its amounts are a floor ("not less than") and s38(1) pays four weekly
wages where SD1's floor is two, so paying s38 satisfies both. The cached determination text
had been in the research directory since P6; a note about a clause is not the clause.

**s40(c) is a FLOOR, not a formula** (D-225). Pro-rata leave for the incomplete cycle is the
GREATER of the ledger's own accrual and the Act's one-day-per-17-worked, because s40(c)(ii)
permits "any basis that is at least as favourable". An employer whose rule set accrues less
generously underpays every leaver and nothing on a payslip shows it, so where the statutory
floor wins the result says so by name. "**Longer than** four months" puts the boundary in the
lower band — exactly four months does not qualify.

**BCEA s84(1) is not implemented anywhere** (O-26): "previous employment with the same
employer must be taken into account if the break between the periods of employment is less
than one year". `service_days_to()` is this-engagement-only by design, and nothing aggregates,
so a re-hired employee's notice band and severance are both understated. D-103 is not the
problem and must not be undone; what is missing is the sum. **O-27** raised too: s40(a)'s time
off in lieu of overtime or Sunday work has no leave type and no ledger, so a mandatory
termination payment cannot be computed at all.

**A negative leave balance is surfaced and never netted off** (D-185, now with its own test):
recovering it is a s34 deduction needing written consent, so the figure reaches the stored
trace and the total is undiminished.

**P7 chunk 6 — the payslip and year-to-date TABLES** (19 September 2026, D-228 to D-231).
1 515 tests green. `payroll_run`, `payslip`, `payslip_line` and `ytd_accumulator` exist, all
four tenant-scoped with `enable_rls()` and picked up by the generated isolation suite (twenty
new cases). **The RUN is still blocked and is not built**: P2 verification gates COMPUTING a
payslip, because unverified reference data is invisible to `in_force_on()` — it does not gate
the tables, their constraints or their triggers, which is the same line P5 and P6 built along.
Nothing generates a payslip, calculates one, or finalises a run.

**Invariant 4 is a trigger, and a FOURTH shape of frozen row** (D-229).
`core/db/rls.py::no_change_when_finalised()` refuses UPDATE *and* DELETE once `is_finalised`
is true. It is none of the other three: a payslip is not append-only (an unfinalised one is
edited every re-calculation, and a test watches that first), it is not a shared catalogue row,
and `no_update_when_locked()` keys on a `status` column. DELETE is refused unlike the
attendance lock, because the reversal of a payslip IS a new payslip and the original has to
survive to be reversed against.

**Invariant 6 is now a CHECK** (D-230): `amount` must equal `amount_exact` rounded to two
places, proven rather than trusted. PostgreSQL's `round()` on numeric rounds a tie away from
zero, which is what `ROUND_HALF_UP` means in Python, so the two agree on negatives too — both
pinned. **Invariant 7 is frozen TEXT beside the foreign keys**: `component_code` and
`source_code` on the line, `employee_snapshot` on the payslip. The FK says which catalogue row
this is; the text says what the line said, and only the second survives a correction.

**`ytd_accumulator` is keyed on the SARS source code**, not the component — three components
are 3601 and the IRP5 states one figure for it — and has no incremental update path at all
(D-153 restated). Only finalised payslips count; a reversing payslip does count, because its
lines are negative and netting them is how a correction reaches the IRP5.

**O-28 raised**: all four tables were designed from the invariants and the existing schema,
NOT transcribed from sheet 02, which was not available to the session that built them. Every
earlier table in this build was reconciled against the workbook. These should be, before the
assembly writes a row.

**P7 chunk 7 — the payroll run and the validation gate** (19 September 2026, D-232 to
D-235). 1 559 tests green. `payroll/runs.py` is the lifecycle and `payroll/validation.py` is
the gate.

**THE P2 GATE IS NOW A REFUSAL RATHER THAN A NOTE, and this chunk is what enforces it.**
`ReferenceDataVersion.in_force_on()` has filtered on `verified_at` and `golden_tests_passed`
since P2's own migration, and `data_current_through` has carried the words "THE STALENESS
GUARD" just as long — and **nothing had ever called either** (`employers/areas.py` is the only
caller of `in_force_on()` in the whole build). Every note saying "the assembly is blocked on
P2 verification" was describing an intention, and an intention with no caller is the silent
guard this codebase keeps shipping. On this database today a run refuses at approval, naming
`verifystatutory`, and a test pins exactly that.

**Transitions are enforced in `transition()`, and the legal moves are DATA** (D-233).
`LEGAL_TRANSITIONS` is a dict, every status change goes through one function, and a test
asserts every enum value appears in the map — a status added and forgotten there would be
treated as terminal by accident. Chunk 6 shipped the test proving the CHECK does *not* police
transitions; this is the other half. **`calculate()` refuses by name**: assembling a payslip
is chunk 8, and a run reporting itself calculated while holding nothing is a run somebody
approves.

**A resolution is against a PROBLEM, not a ROW** (D-234) — keyed on (code, employee) for the
run, because issues are derived and rewritten and the first design let a resolution last
exactly until the next validation. Caught by its own test. The reason is mandatory and the
person named, held by a CHECK guarded both ways over the nullable columns. `approve()`
re-validates rather than trusting an earlier pass.

**Finalisation does five things in one transaction** (D-235) and reads the snapshot at the
PERIOD's date, not today's — `current_pay_basis` is a cache refreshed as at today (D-107), and
today is not the date the payslip is for.

**P6 — Leave: ALL FIVE CHUNKS BUILT** (18 September 2026). Chunk 5 — maternity, parental
under Van Wyk and adoption — is in: the two totals as cited reference data, the declaration
captured rather than computed, s25(4B)'s single sequence enforced, and the two opposite lapse
behaviours loaded as data (D-201 to D-206).
1,262 tests collected, all passing. An unpaid leave day never also spends leave: an overdraw
(D-188), uncertified sick leave (D-196) and an unauthorised absence elected unpaid (D-195)
all charge nothing. See D-186 to D-197 for 4b and 4c. The leave
catalogue, cycles, the append-only ledger, the accrual engine (ANNUAL, SICK and
FAMILY_RESPONSIBILITY as of chunk 4), evidence, applications and authorisation are in —
forfeiture *capture* is chunk 3, and it is not stubbed here. **Chunk 5 — maternity,
parental leave under the Van Wyk interim reading, and adoption — has not started.**

`leave_type` is seeded via `manage.py seedleavetypes` (`--list` shows the catalogue without
touching the database), in the `seedcomponents` mould: idempotent, never updates, and every
shared row a system row (D-134's own CHECK pair). Two shapes are FLAGGED rather than settled —
`STUDY` and `COMPASSIONATE` are not BCEA leave at all, so every one of their shape flags is a
discretionary default — and two more are flagged at the field level inside otherwise-settled
rows: `FAMILY_RESPONSIBILITY.requires_evidence` and `ADOPTION.requires_evidence`, because s27(4)
and s25B condition proof on the employer asking rather than mandating it outright. `leave/types.py`
carries the full reasoning per code, cited against the Act section that produced it.

Five decisions were settled with Kobus ahead of this chunk and are recorded as D-162 to D-166:
the rule set's accrual method applies unless `employee_leave_entitlement.accrual_method` says
otherwise, since two of BCEA s20(2)'s three methods need agreement (D-162); `leave_cycle`
anchors to the CURRENT engagement's own start date, and a re-hire starts fresh at cycle 1 with
the prior engagement's cycle never revived (D-163); a balance is stored in whatever unit its own
accrual produced — days or hours — and nothing in `leave/` ever converts between them (D-164);
forfeiture is never automatic anywhere in this codebase, the transaction type exists for enum
completeness only, and there is correspondingly no cap on carried leave (D-165); and this chunk
computes days and hours, never money — the leave rate is P7's (D-166).

**D-163 required a real fix, not only a docstring.** `leave_cycle`'s EXCLUDE constraint is
correctly scoped to `(employee, leave_type)`, not to the engagement — so a re-hire's fresh cycle
1 calendar-overlaps a prior engagement's own still-open 12-month cycle unless that prior cycle's
date range is itself truncated at termination. `leave/cycles.py::_close_cycles_from_other_engagements()`
does this lazily, the next time `ensure_cycles()` runs: it shortens the old cycle to end the day
after its engagement's termination date and marks it CLOSED, never touching its balance columns.
Found by a failing re-hire test written before the mechanism existed — the same lesson D-131
taught the first time an EXCLUDE constraint's actual scope did not match what a decision assumed.

**`leave_transaction` is the append-only ledger, and its sign convention is enforced by a CHECK,
not only stated in a docstring**: `accrual`/`opening_balance` positive, `taken`/`payout`/
`forfeiture` negative, `adjustment` either sign with a mandatory reason, `reversal` the exact
negation of the row it corrects. `core/db/rls.py::append_only()` backs the table by trigger, the
same lesson P0 already learned about a table's owner not being bound by `REVOKE`.
`leave/ledger.py::reverse_transaction()` refuses a reversal of a reversal by name — the reversal
IS the correction, so there is nothing further to undo. **Deviates from this chunk's own brief,
in the workbook's favour (D-167)**: `TransactionType` carries the workbook's seven values —
`opening_balance` and `payout` where the brief said only `termination_payout` — not the brief's
six. Its physical shape changed again in chunk 2 — see D-170 below; the sign convention and the
seven-value enum are unaffected.

**The accrual engine is scoped to `ANNUAL` only this chunk (D-168).** Calling it for `SICK`, or
any other `accrues=True` type, raises `AccrualNotSupportedError` naming the gap: BCEA s22(2)'s
"first six months" sick accrual genuinely accrues off attendance the same way the per-17-hours
annual method does, but the SIX ITSELF is a threshold with no home yet in `leave_rule_set` —
only the accrual ratio is stored, not the duration of the window it governs — and "resolve raises
when a figure is missing" is applied here to the threshold a rule would need, not only to a rate.
Straight-line monthly, per-days-worked and per-hours-worked are all implemented in full for
annual leave, read from `leave_rule_set` through `statutory.resolve`, keyed off the employee's
own 5-day/6-day schedule or off P5's attendance rows — this is why P5 had to come before P6.
`leave_accrual_run`'s own `UNIQUE (employer, leave_type, accrual_as_at)` is the idempotency
guarantee; `run_monthly_accrual()` checks for an existing COMPLETED run first so the ordinary
case never reaches the constraint at all.

**AMENDED in chunk 4 (D-181, D-183): SICK and FAMILY_RESPONSIBILITY are no longer behind
`AccrualNotSupportedError`.** `SICK_LEAVE_FIRST_PERIOD_MONTHS` (the six-month threshold
this paragraph named as missing) is now a `statutory_parameter`
(`reference/ref-2026.03.01-sick-accrual.json`), and `leave/accrual.py` dispatches by leave
type code — never a parallel run mechanism, still the same `run_monthly_accrual()` entry
point. SICK's cycle 1 runs the attendance-driven ratio phase this paragraph describes,
then ONE top-up transaction at the six-month mark to the full six-week-equivalent LESS
what was taken, proven as the cycle's TOTAL balance rather than the top-up's own delta —
see D-181's arithmetic proof. Cycle 2 onward is granted the six-week-equivalent upfront,
once, since a second 36-month sick cycle can never fall inside the first six months of
employment. FAMILY_RESPONSIBILITY is granted once, upfront, per cycle (D-183) — never
accrued monthly, never carried over, never paid out. `AccrualNotSupportedError` still
applies to MATERNITY, PARENTAL and ADOPTION, chunk 5's own work.

**THE NEGATIVE TEST THAT MATTERS is named exactly that in `leave/tests/test_accrual.py`** —
`test_no_forfeiture_transaction_is_ever_written_automatically` runs the engine eighteen months
forward, six months past cycle 1's own twelve-month end, and asserts zero forfeiture
transactions exist anywhere and cycle 1's balance is exactly its full accrual, untouched. It is
the guard that catches a regression the moment anything in a future chunk tries to make
forfeiture automatic, and per Kobus's own instruction it is named so nobody deletes it by
accident.

**`leave_cycle`, `leave_transaction` and `leave_accrual_run` shipped with RLS ENABLED ON NONE OF
THEM (D-169) — a sixth instance of the exact failure the Non-negotiables section above already
lists five of.** Every test written against the three tables up to that point ran inside one
pinned tenant context and read back exactly the row it had just written, which is
indistinguishable from correct isolation until a second tenant is in the room — RLS with no
policy denies every row rather than exposing any of them, so the tables read as isolated while
providing none at all. Found by the generated isolation suite exactly as it is meant to catch a
new model the day it is written, and fixed in migration `0005` without touching `0003`'s own
`append_only()` trigger.

**Chunk 2 opened by reconciling `leave_transaction` against sheet 02 BEFORE building anything on
top of it (task 0, D-170).** Sheet 02 names a NOT NULL `days` and a nullable `hours`; read
literally, every row would need a days figure, which for an hourly-accrual employee can only come
from converting hours against the employee's own schedule — exactly the silent conversion D-164
exists to prevent. Both `days` and `hours` are nullable instead, with a CHECK proving exactly one
is ever populated — sheet 02's own column names, chunk 1's own data shape. **The sign-check CHECK
needed an explicit `__isnull=False` guard on every branch**, because PostgreSQL treats a CHECK
expression that evaluates to NULL as SATISFIED, not violated — an unguarded
`Q(transaction_type="accrual", hours__gt=0)` on a row whose `hours` IS NULL evaluates NULL, and
ORed against another FALSE branch leaves the whole CHECK NULL, which passes a wrong-signed row
through silently. `leave_transaction.leave_application` and `attendance_day.leave_application` both
become real FKs this chunk too (D-175), now that `leave_application` exists.

**`leave_evidence_type` (task 1) carries no tenant field and inherits none of the three tenant
bases (D-173)** — pure reference data, `sector`'s own shape, not `leave_type`'s. Guarded by
`core/db/rls.py::no_delete()` regardless, since `leave_application` (FORCE RLS) points at it —
D-76's lesson, applied on arrival rather than found by accident afterward. Its one figure, BCEA
s23(1)'s "more than two consecutive days" certificate threshold, is a new `statutory_parameter`
row (`SICK_CERTIFICATE_MAX_CONSECUTIVE_DAYS`), read through `statutory.resolve` — D-100/D-101's
own precedent for a single citable threshold, not a whole rule set and not a Python literal
either. **Evidence gates PAY, never LEAVE**: BCEA s23 conditions the employer's right to withhold
pay on an unevidenced absence beyond the threshold, never the right to take the leave at all, so
`leave/evidence.py` and everything built on it decide `is_paid`, never whether the application
exists. `DOCTOR_NOTE`/`CLINIC_NOTE` pay regardless of length; `SELF_CERTIFIED`/`NO_NOTE` pay only
within the threshold. FLAGGED: the exact boundary between `SELF_CERTIFIED` and `NO_NOTE` is a
per-application computation, not fixed by the catalogue row alone.

**`leave_application` / `leave_application_day` (task 2) carry three settled rules together
(D-174).** Evidence gates pay, restated at the application layer: a sick application with no
certificate, however long, always reaches `submitted` — never refused. **An overdrawn application
is never refused either** — `leave/applications.py::submit_application()` caps the deduction at
what the ledger actually holds and marks the uncovered days `is_paid=False` /
`deducted_from_balance=False`, recording `exceeds_balance` and `unpaid_days` rather than paying
what is not there. **A week's leave over a public holiday costs four days, not five** —
`is_working_day` is FALSE for a rest day AND a public holiday inside the span, and neither
deducts; both still get their own row so the audit can show why. Half days are the minimum
increment for a salaried basis (`day_portion`); an hourly-accrual employee's application deducts
`hours` instead and never converts (D-164, still). FLAGGED in the module's own docstring, at the
time: a `balance_source='parent'` type (`ANNUAL_UNAUTHORISED`) is not resolved to its parent's
cycle here, and an hourly employee's own overdraw has no `unpaid_hours` column to summarise into.
**AMENDED in chunk 4 (D-180): `ANNUAL_UNAUTHORISED` now resolves.**
`leave/cycles.py::resolve_balance_leave_type()` is the one place a `balance_source='parent'`
type resolves to its `parent_leave_type`, called first by `ensure_cycles()`, `current_cycle()`
and `accrual_method_for()` — so an unauthorised-absence transaction posts against ANNUAL's own
cycle and reduces ANNUAL's own balance, while the ledger row still carries its own
`leave_type=ANNUAL_UNAUTHORISED` as the audit label. `unpaid_hours` now exists (D-188).

**Self-approval is BLOCKED and escalates to the owner (task 3, D-174).** Only where the deciding
user IS the owner AND no other approver (owner or admin) exists for the tenant may
`self_approved` be TRUE — and then only with a mandatory `self_approval_reason`, visible in the
leave register and never hidden; `leave/authorisation.py::approve()` refuses outright otherwise,
naming who may decide instead. **Approval writes the `taken` ledger transaction and the
`attendance_day` row together, in one transaction** — refusing, naming the date, if a day in the
span is already captured as worked (nobody is both at work and on leave), and refusing, naming
the run, if a day is locked by a finalised payroll run (`attendance/capture.py`'s own guard, not
duplicated). **Cancelling REVERSES the ledger and removes the attendance days it wrote — it never
edits or deletes a ledger row** (invariant 4), permitted even after the leave was already taken:
the employer may have to explain a late cancellation, and that is exactly what the trail is for.

**A real, substantive gap surfaced and was closed while building part-day applications
(D-171).** `calculators/attendance.py`'s `days_worked_equivalent` for a `LEAVE` day used to be a
flat `Decimal(1)` regardless of length — D-149 (chunk 1's own hardening pass) had already flagged
this exact gap and said P6 must revisit it before the leave FK became real. `AttendanceDayInput`
now carries `leave_day_portion` (default 1, every existing caller unaffected), and a half day of
approved annual leave correctly yields `0.500`, not `1.000`, for both daily-rate pay and leave
accrual.

**Cycle-closing moved to where the event happens (task 5, D-172).** Chunk 1 only ever closed a
prior engagement's open cycle LAZILY, the next time `ensure_cycles()` ran for a re-hire —
D-132's own lesson restated: a boundary that moves when somebody happens to look is one that is
wrong in between. `employees/engagements.py::terminate()` now calls
`leave/cycles.py::close_cycles_at_termination()` directly, via a deferred (function-body) import
— `leave.cycles` already imports from this same module at ITS OWN top level, so a top-level
import here would be a circular import at Python's own load time, not merely a layering
preference. The lazy path stays as the backstop, not the mechanism.

**Chunk 3 closes the phase, and the last task is proving the phase's own definition of done
rather than asserting it.** Before chunk 3's own work, one documentation debt was paid first:
CLAUDE.md's non-negotiables used to document the NULL-escapes-a-unique and
NULL-escapes-an-exclusion findings as two separate traps. They are the same PostgreSQL
behaviour, now stated once — "every constraint over a nullable column is permissive by
default" — with the CHECK-evaluates-to-NULL case (found closing chunk 2, D-170) folded in
as the third face of it rather than left implicit.

**Forfeiture capture is built exactly as Kobus decided it, in chunk 1, and restated here
(D-177): manual, and NOTHING else.** `leave/forfeiture.py::capture_forfeiture()` is the
whole mechanism — an employer names a cycle, a quantity and a reason, and it writes one
negative `forfeiture` transaction attributed to the capturing user. Refuses a forfeiture
larger than the cycle's own (recomputed, never cached) balance, naming both figures;
refuses one against a cycle with nothing to forfeit; reversible like anything else in the
ledger. **Chunk 1's `test_no_forfeiture_transaction_is_ever_written_automatically` is
UNCHANGED and still passes** — a dedicated test in this chunk inspects it by name rather
than merely trusting nobody touched it, because "trust me, I didn't edit it" is not a proof.

**The forfeiture deadline warning (D-178) is a query, not a screen and not a job** —
`leave/warnings.py::forfeiture_warnings()` writes nothing, ever, because chunk 1 and chunk 3
both refuse to let anything automatic near a forfeiture transaction. For an employer, every
ANNUAL cycle that has ended with a balance still outstanding, bucketed
approaching/due/past against `cycle_end + leave_rule_set.annual_leave_forfeit_months` — the
six months read through `statutory.resolve`, never a literal, exactly as the brief demanded.
`DUE_WINDOW_DAYS = 30` is NOT that statutory figure; it is a plain reporting judgement call
about escalation urgency INSIDE the six months, flagged for O-06 alongside this chunk's other
modelling choices rather than dressed up as a citation it is not. Excludes a terminated
employee's CLOSED cycle on purpose — that is a BCEA s40(b) termination-payout question, a
different duty this list must not conflate with forfeiture.

**`public_holiday_observance` (D-179) changes chunk 2's own answer, on purpose.** An employer
whose observance row says `is_observed=FALSE` has its employees work that date as ordinary —
so it BECOMES a working day and IS deducted from leave, on the same calendar date a different
employer's employees still get free. `leave/applications.py::_is_observed_holiday()` checks
this table before the statutory calendar, in either direction; a NULL `public_holiday` lets an
employer declare a day the calendar knows nothing about. Proven both directions, same date, two
employers, one test. **COMPLIANCE NOTE, in the model's own docstring and flagged for O-06**: a
row with `is_observed=FALSE` RECORDS an agreement under BCEA s18(3) — it does not MAKE one, and
nothing here checks that a genuine agreement stands behind it.

**Task 5 asked for P6's own definition of done to be PROVEN, not asserted, and it found
something real.** `leave/tests/test_reconciliation_property.py` uses Hypothesis to generate
random sequences — accruals across multiple cycles, applications approved and cancelled (full
day and part day), adjustments with reasons, forfeitures, reversals of any of them — and after
every single step confirms the balance equals an independent sum of the ledger exactly, is
never negative, and that `leave/balances.py`'s own staleness-driven cache agrees with that
independent sum. It tests "never negative" as an UNQUALIFIED property, stronger than the
brief's own "except where an overdrawn application explicitly made it so" — because this
codebase's chunk 2 design (D-174) never actually lets an application drive a balance negative
in the first place; the qualifier's own premise does not arise here. **It DID find one genuine
interaction (D-176)**: reversing an EARLIER transaction after a LATER adjustment or forfeiture
has already relied on its own contribution being present is correct, order-independent ledger
arithmetic that can legitimately leave a cycle showing a deficit — the ledger doing exactly
what a signed sum should do, not a bug in `leave/ledger.py`. Fixed in the TEST's own definition
of a valid sequence (skip a reversal that would drive its cycle negative, the same treatment an
overdrawn adjustment already gets), not in the ledger, which was never asked to understand
causality between independent rows.

**What that proof covers as of chunk 4c:** ANNUAL, SICK and FAMILY_RESPONSIBILITY, both denominations, through the real engine (D-190). It does not cover MATERNITY, PARENTAL or ADOPTION (chunk 5), `ANNUAL_UNAUTHORISED`, or termination payout (P7). Detail in `docs/PHASES.md`'s P6 section.

**Chunk 4: D-176 was the right diagnosis and the wrong fix, and this chunk corrects the fix
rather than the diagnosis (D-184).** Chunk 3 responded to the property test's own finding —
reversing an earlier transaction after a later one already relied on its contribution can
legitimately leave a cycle negative — by excluding that sequence from the test's own generator.
That preserved a false invariant ("no balance is ever negative") by removing the coverage that
disproved it; in production the sequence still runs and the balance still goes negative, with
nothing surfacing it. This chunk restores the generator and replaces the invariant with the one
that is actually true: the balance always equals the ledger sum exactly, and every negative
balance is fully attributable to identifiable rows. `leave/negative_balances.py` is the query
that proves the second half — same shape as `leave/warnings.py`'s forfeiture list (read-only, no
job, no screen), but not scoped to ANNUAL, since a negative balance is not a leave-type-specific
statutory question. **Recorded against P7, not built here (D-185): a termination payout must
never net a negative leave balance off the final payment** — recovering it is a BCEA s34
deduction requiring the employee's consent, and P7 must surface this query's own figure for a
human decision, never subtract it automatically.

**Chunk 4 also closed the property test's own remaining HOURS gap (task 5).** The same
Hypothesis generator now runs twice — once against a DAYS-denominated ANNUAL cycle, once against
an HOURS-denominated one, produced by giving the fixture employee a `PER_HOURS_WORKED`
`EmployeeLeaveEntitlement` override before any cycle is generated — with every action posting
and reading whichever physical column (`days` or `hours`) the cycle it touches actually carries,
via `cycle.unit` itself. No conversion between the two is performed anywhere in the test — D-164
still holds — because a test that converted would quietly bless the exact thing that decision
forbids. Zero drift found in either denomination, run at 250+ examples during development.

**A hardening fix, found while building SICK's own ratio phase, reaches ANNUAL's two
attendance-based methods too (D-182).** `leave_transaction.days`/`.hours` are
`DecimalField(decimal_places=3)`, and an un-quantized division carries Python's full
~28-significant-digit context precision — `full_clean()` refuses the row outright the moment the
division is not exact. `_per_days_worked_quantity` and `_per_hours_worked_quantity` (chunk 1's
own ANNUAL methods) carried this same latent defect, untested because every existing test
divided evenly; all three ratio-based quantity functions now quantize to `Decimal("0.001")` with
`ROUND_HALF_UP`, the same rounding `leave/applications.py` already applies to a part-day's hours.

See `docs/PHASES.md` for the task breakdown.

Nothing is deployed. There is no customer data. This is the right moment to be rigid
about the invariants above, because retrofitting any of them later is a rewrite.
