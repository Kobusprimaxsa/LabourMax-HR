# Work breakdown

Twelve phases, roughly 51 developer-weeks sequential and 44–46 with the overlaps noted.
Estimates assume one developer working with AI assistance and include testing, but not the
parallel run's calendar time.

Each phase lists its tasks in dependency order, the tables it delivers, and a definition of
done that is testable rather than aspirational. **A phase is not finished until its
definition of done passes.**

Table counts and column detail come from the Database Specification workbook, sheet 01.

---

## P0 — Foundation & Tenancy · 3 weeks · 12 tables

Nothing else can start until tenant isolation is proven.

- [x] Virtualenv, dependencies, local PostgreSQL, `.env` from `.env.example`
- [x] Django project boots, `manage.py check` clean
- [x] `TenantScopedModel`, `TenantOptionalModel`, `AuditMixin`, `TimestampedModel` base classes
- [x] `TenantScopedManager`, `TenantOptionalManager`, `all_tenants` escape hatch,
      `tenant_context()` and `platform_context()`
- [x] `TenantContextMiddleware` pinning the tenant to request **and** database session
- [x] `core/db/rls.py` helpers; every tenant table's migration calls `enable_rls()` or
      `enable_rls_optional()`
- [x] `manage.py dbcheck` — connection, extensions, and a hard refusal if the app role
      is a PostgreSQL superuser
- [x] Models: `platform_setting`, `tenant`, `app_user`, `tenant_membership`,
      `user_invitation`, `tenant_ownership_transfer`, `otp_challenge`, `login_audit`,
      `audit_log`, `file_object`, `background_job`
- [x] Custom user model wired as `AUTH_USER_MODEL`, Argon2id hashing
- [x] Max-two-admin-users rule: column default, database trigger, application check
- [x] Audit log signal wiring — field-level diffs, masked sensitive fields
- [x] File storage abstraction with virus-scan status gate
- [x] Generated tenant isolation suite passing
- [x] CI green: lint, format, system checks, migration check, isolation suite, full tests

**Done when:** an automated cross-tenant leakage suite runs on every commit and passes,
and RLS is enabled *and forced* on every tenant-scoped table.

**Status: COMPLETE, 13 September 2026.** 124 tests passing locally and on CI, against PostgreSQL 18 and Python 3.14.

Four defects of one shape were found and fixed during this phase, all of them
protection that read convincingly in the source and did nothing at runtime: five
models carrying a tenant column with no scoped manager or policy; `tenant_context()`
never setting the database session variable, so RLS was dormant in every test and
task; `append_only()` being a `REVOKE` against `PUBLIC`, which does not bind the
table owner; and service functions querying tenant-scoped rows with no tenant pinned,
where RLS returns zero rows rather than an error. See `CLAUDE.md`.

A fifth was caught by the suite itself on its first CI run: the `postgres` Docker image
creates `POSTGRES_USER` as a superuser, and a superuser bypasses row-level security, so
CI was running the whole isolation suite against a connection that could see everything.
`test_application_role_is_not_a_superuser` is the only reason that surfaced as four
failures rather than as a green badge asserting protection that was never enforced.

---

## P1 — Registration, Subscription & Billing · 4 weeks · 13 tables

Can overlap P2 and P3.

- [ ] Plan catalogue: `subscription_plan`, `plan_feature`, `plan_price_band`
- [ ] Pricing engine: `base + MAX(0, headcount − threshold) × per_head`, both models
- [ ] `service_agreement_version` and the disclosure sentence template
- [ ] Public registration: email + mobile verification, both mandatory
- [ ] Plan selection; declared-headcount dropdown for contract cleaning
- [ ] Disclosure sentence rendered above the button with real amount and both real dates
- [ ] `tenant_service_agreement` written at acceptance — dates computed **once**
- [ ] Payment gateway adapter behind an internal interface; tokenisation only
- [ ] `gateway_webhook_event` — write before processing, idempotent on the gateway's event id
- [ ] Trial-end charge job: 09:00 SAST, re-read status, idempotency key, never before date
- [ ] Recurring cycle anchored to `min(first charge day, 28)`
- [ ] `invoice`, `invoice_line`, gapless numbering per financial year
- [ ] `billing_period_usage` — four metrics recorded, one billed
- [ ] Band-exceed prompt with inline adjustment, recorded as `subscription_event`
- [ ] Dunning ladder and suspension; read-only access preserved
- [ ] Second-admin invitation flow

**Done when:** a stranger can register, pay and land in a working empty account with no
manual step, and a cancellation at 23:58 on the cancel-by date is never charged.

---

## P2 — Statutory Reference Data · 3 weeks · 20 tables

The most important phase in the build. Can overlap P1.

- [x] `sector`, `sector_area`, `municipality_area_map`, `job_grade`
- [x] `minimum_wage_rate` with exclusion constraints on overlapping ranges
- [x] `tax_year`, `paye_tax_bracket`, `paye_rebate`, `medical_tax_credit_rate`
- [x] `statutory_parameter` — UIF, SDL, COIDA, BCEA threshold, VAT
- [x] `leave_rule_set`, `working_time_rule_set`, `termination_rule_set` per sector
- [x] `public_holiday` with the Sunday-shift rule
- [x] `sars_source_code`, `bank`, `bank_branch`
- [x] `reference_data_version` with verification, golden-test flag, `data_current_through`
- [x] `statutory_watch_item` seeded with the maintenance calendar
- [x] `statutory/resolve.py` — every effective-date lookup, in one place (D-58)
- [x] Loader: staged import, citation **mandatory** on every row, nothing ever updated (D-56, D-57)
- [x] `manage.py loadstatutory` and `manage.py verifystatutory` — two commands, two people
- [x] Staleness guard blocking payroll runs beyond `data_current_through`
- [x] The no-hard-coded-rate rule as a test rather than a grep (D-59)
- [x] `statutory/checks.py` + `manage.py checkstatutory` — the loaded data reconciles with
      itself, and verification is refused until it does (D-62)
- [x] First data load: `reference/ref-2026.03.01.json` — wage floors, SARS 2027 tax year,
      contribution parameters, public holidays 2026 and 2027, the watch calendar. 61 rows,
      each with a citation, loaded and unverified
- [ ] **Kobus verifies every figure against its source**, then `verifystatutory`
- [x] Rule sets, BCEA default and domestic (SD7): `reference/ref-2026.03.01-rules.json`
- [x] Contract cleaning rule sets from SD1: `reference/ref-2026.03.01-sd1.json` (D-67)
- [x] Contract cleaning **Area B** (KwaZulu-Natal): the BCCCI Main Collective Agreement,
      GN R.7296 in GG 54412, 27 March 2026 — loaded 19 Sep 2026 as its own version
      `ref-2026.04.01-bccci.json` (D-237). Three wage rates including the system's first
      FUTURE-DATED rows, and clause 3's 4,33 monthly factor scoped to the area (D-236).
      Area B resolution now REFUSES rather than falling back to the NMW (D-238).
      **Still open: the predecessor agreement covering 1–31 March 2026 (O-30).**
      (The heading said Area C; D-118 corrected the lettering and Area B is KwaZulu-Natal)
- [x] `sars_source_code` — 19 codes with cited base flags (D-69, D-70, D-71)
- [x] The bank list and branch codes — 25 banks, lengths deliberately unloaded (D-72, D-73)
- [x] Source codes 3901 and 3907 from SARS PAYE-AE-06-G06, closing the severance gap (D-114)
- [ ] Golden tests reproducing the published SARS and DEL worked examples

`source_url` is deliberately optional rather than mandatory as this plan originally
specified. Not every gazette is online, older determinations are not, and a required
URL field is a field people fill with something plausible. A citation you can find in
a library beats a URL that 404s. `source_reference` is what is mandatory, and a CHECK
constraint refuses a blank one on every cited table.

**Done when:** every statutory number lives in a table with a gazette citation, and
`grep -r` finds no hard-coded rate anywhere in the codebase.

---

## P3 — Employer Setup · 2 weeks · 7 tables

- [x] `employer`, `employer_statutory_registration`, `employer_bank_account`
- [x] `core/db/fields.py` — encryption at rest, unsearchable by design (D-77)
- [x] Reference tables refuse DELETE by trigger — FORCE RLS defeats the foreign key (D-76)
- [x] `workplace` with client name, contract reference, area resolution (D-81)
- [x] `employer_setting`, seeded from sector at onboarding (D-79, D-80)
- [x] **Municipality-to-area data** — the twelve Area A municipalities the determination
      names are loaded; Area B is a province and Area C a residual, so neither is a list (D-118)
- [x] `pay_group` and the pay period generator, all five frequencies (D-82, D-83, D-84, D-85)
- [x] `payroll_component` catalogue, sixteen system components seeded and locked
      (D-87 shared tenancy, D-88 no rate lives here, D-89 flags follow the source code,
      D-90 severance inactive, D-91 the leave pay determination, D-93 the system row lock)

**Done when:** an employer completes onboarding and generates a full year of pay periods.

---

## P4 — Employee Master File · 6 weeks · 16 tables

- [x] `employee` with encrypted ID number, hash-based duplicate prevention, sort columns
      (D-95 the hash is scoped per tenant, D-96 checksum + date cross-check, D-97 the
      unique is per tenant, D-98 email is not CITEXT)
- [x] ICU collation set in the creating migration
- [x] `employee_address`, `employee_contact`
- [x] `employee_engagement` — start, termination, fixed-term end; re-hire support (D-103)
- [x] BCEA s43 minimum age, as reference data rather than a literal (D-100, D-101, D-102)
- [x] `employee_position` with site assignment (single or multi), EXCLUDE on the date range
- [x] `employee_remuneration` — five pay bases, derived rates computed once and stored (D-104, D-106)
- [x] Minimum-wage validation at capture, raising and acknowledgeable (D-108)
- [x] Pay cache maintained on write; the nightly job is the remaining half (D-107)
- [x] `employee_bank_account` with active and end dates, plus the ghost-employee hash (D-111)
- [x] `employee_tax_profile`, `work_schedule`, `work_schedule_day` (D-109, D-110)
- [x] `employee_leave_entitlement`, `employee_recurring_component`, `employee_note`
      (D-127 brings `leave_type` forward from P6; D-128 derives the BCEA s34 consent rule)
- [x] `document`, `document_category` — four-way attachment arc, visibility whitelist
      (D-138 the third shared table plus `is_system`, D-139 the arc implemented verbatim
      and asymmetric, D-140 the seeded tenant/employer categories attach to the employer).
      Out of scope and not built: the 20MB upload cap, server-side image downscaling and
      the per-plan storage quota (need P1's `plan_feature`), and the 60/30/7-day expiry
      alert job (P9). `document_category.is_confidential_by_default` is left FALSE on
      every seeded row — which categories should hide from the read_only role is a policy
      call sheet 03 does not make, and nothing here fabricates one.
- [x] Current-state cache columns, maintained on write **and** by a nightly job —
      `employees/currentstate.py` + `manage.py refreshemployeecache`. D-132 moved the
      whole as-at set onto the date it belongs to: a termination captured in advance
      no longer closes the engagement, terminates the employee or stops the billing
      six weeks before they stop working
- [x] Employee list: grouped by pay group, sector-derived default sort, remembered per
      user — `employees/listing.py`. The per-user memory is D-133,
      `tenant_membership.ui_preferences`, a registry-checked bag. Service layer and
      tests; the screen itself belongs with the UI phase
- [x] `employee_import_batch` — bulk import from a generated .xlsx template, on the
      preview-validate-apply-reverse pattern, running every validation the single-capture
      path runs (D-122, closes O-11). ADDED to P4 scope, +1 week. NOT in the workbook
      (D-144), modelled on `attendance_import_batch`. `employees/importing.py` is preview
      IS apply, rolled back (D-145); a below-minimum row is a batch-wide warning needing
      a named acknowledger (D-146); the template reads the same column spec the importer
      parses against (D-147); the source file's content is purged once applied or
      reversed (D-141) and `employee.created_by_import_batch` is the provenance column
      (D-142)

**Done when:** capturing an employee below the sectoral minimum raises a visible, logged
exception, and a future-dated increase flips the cache on its own effective date. Both
halves passed on 14 September 2026. Forty employees
import from a spreadsheet with every one of those checks applied, and the batch reverses
as a unit. **All three passed on 14 September 2026 — P4 COMPLETE**, 824 tests green.

---

## P5 — Attendance & Time · 4 weeks · 3 tables

- [x] `attendance_day` with hour bucketing: ordinary, overtime, Sunday, public holiday, night
      (chunk 1). `calculators/attendance.py` is the first real calculator — every figure
      read from `working_time_rule_set`, none hard-coded. `attendance/capture.py` computes
      and stores the buckets from the rule set in force on the work date. Three
      hour-bucketing shapes (D-148) and `days_worked_equivalent` (D-149) are modelling
      choices flagged for the labour law review (O-06); there is deliberately no
      golden-file test (D-150). Locking is a trigger (D-151), not an application check
- [ ] Monthly capture grid — sticky headers, keyboard navigation, colour-coded day types.
      The service the screen will call exists (`attendance/grid.py::month_grid`, chunk 2);
      the screen itself does not — no views, no templates, this codebase still has none
- [x] Pre-fill from schedule for salaried bases **only**; hourly and daily open blank
      (chunk 2, D-25 already settled the distinction). A pre-fill proposal is never a
      saved row
- [x] Bulk actions filling empty cells only (chunk 2, `attendance/grid.py::bulk_fill`) —
      through `attendance.capture.capture()` for every date, never a second write path
- [x] `attendance_import_batch` — preview, validate, apply, reverse (chunk 3). Unlike the
      employee import, this one routinely REPLACES a day that already exists — re-importing
      a corrected file is the ordinary case. A locked day is refused by name in the row
      loop, so preview reports it rather than a trigger dying at apply; an approved day is
      refused unless `allow_replacing_approved=True` is passed. Reverse restores a replaced
      day to its exact prior values (a new `prior_state` JSONB column, not in sheet 02 —
      D-156) and deletes only the days it created. Raw capture only: the template carries
      none of the five hour buckets, proven against `EXPECTED_COLUMNS` itself. The bulk-
      import mechanics shared with the employee import now live in `core/importing.py`
      (D-155)
- [x] `timesheet_summary` aggregation with staleness flag (chunk 2). A cache and nothing
      more (invariant 3) — recomputation always rebuilds from `attendance_day`, no
      incremental path. Staleness is two layers, deliberately independent: a signal
      (D-153) and a ground-truth function that does not trust the signal's own flag
- [x] Live exception panel's approval gate: `attendance/approval.py::approve()` (chunk 2)
      refuses, naming each one, while a BLOCKING exception from chunk 1's
      `evaluate_exceptions` stands over the span; a warning does not block. Daily and
      weekly overtime caps, and the meal-interval and rest checks sheet 03 also asks for,
      are covered. Consecutive sick days are P6 (a leave application to check against)
      and are not stubbed. The panel screen itself does not exist yet
- [x] `attendance/completeness.py::missing_attendance_days` — the P7 hook (chunk 2).
      Attendance-driven bases only; a salaried employee with no row is assumed to have
      worked the day, not flagged missing. P7's gate will call this and is not built here

**Done when:** a month for twenty employees is captured in under ten minutes, and an
uncaptured attendance-driven day blocks the payroll run.

**Not yet demonstrated, and not ticked as met.** Every table and service the phase specified
is built and tested (chunk 3 closes the build), but both halves of this criterion need
things that do not exist yet: the ten-minute claim needs the capture screen (no views, no
templates, anywhere in this phase), and the payroll-blocking claim needs P7's validation
gate calling `attendance/completeness.py::missing_attendance_days()` — the function is
correct and ready, but nothing calls it because there is no payroll run yet. This is a
completed prerequisite, not the outcome the "Done when" clause actually asks for.

---

## P6 — Leave Management · 5 weeks · 8 tables — chunks 1 to 5 built (4b, 4c hardening)

**Chunks 1 to 4 done** (15 September 2026): the catalogue, cycles, the ledger, the
accrual engine (now ANNUAL, SICK and FAMILY_RESPONSIBILITY), evidence, applications,
authorisation, manual forfeiture capture, the forfeiture deadline warning, public
holiday observance, the negative balance report, and `ANNUAL_UNAUTHORISED`'s parent
balance. **Chunk 4b** (16 September 2026, D-186 to D-190): s22(4) as an employer election,
the statutory data commands in CI, `unpaid_hours`, s27(1) eligibility enforced, and the
property test extended through the real engine to SICK and FAMILY_RESPONSIBILITY — where it
found two defects, both fixed. **Chunk 4c** (17 September 2026, D-191 to D-194): seven
tests lost to a parallel session restored with the four guards they needed; a statutory
accrual method refused at capture by trigger; the overdraw proven to retain held leave, and
two other unpaid paths raised as defects awaiting decision (D-193); SD7 and SD1 read from
the clause text. See the P6 section of CLAUDE.md and D-162 to D-194 for the detail.

**Chunk 5 is BUILT** (18 September 2026, D-201 to D-206): maternity, parental and adoption
leave under the interim reading-in in *Van Wyk and Others v Minister of Employment and Labour*
[2025] ZACC 20. The two totals (four months; four months and ten days in the aggregate where
both parties are employed) are cited reference data resolved on the application date; the
relationship shape and the employee's share are captured as a declaration, never computed;
s25(4B)'s single sequence is enforced per birth or placement; and the suspension's two opposite
lapse behaviours are loaded as data. These types still do not accrue — `AccrualNotSupportedError`
is correct for them and stays.

**What P8's UI-19 will need from this data, so the shape is right now rather than retrofitted
(task 7d).** `statutory_out` is not built. When it is, the declaration written here is what the
UI-19 and the UIF benefit claim will read: `leave_application.parental_event_date` (the birth,
placement or adoption-order date), the start and end of each approved period, and
`parental_relationship_shape` — the employee claims against the UIF, and the employer's
declaration is what supports the claim. Two things it will NOT find here, deliberately: the
other parent (D-202 — never captured, and unknowable), and the child's identity details (only
the event DATE is stored). **And the UIF Act half of Van Wyk is unread-in:** order para 2
declares ss 24, 26A, 27 and 29A of the Unemployment Insurance Act invalid alongside the BCEA
sections, but para 5 reads in only the BCEA, so the benefit periods a UI-19 declares are still
governed by the unamended UIF Act during the suspension (D-203). P8 must not assume the BCEA
leave period and the UIF benefit period are the same length. Read the honest "Done when" assessment at the bottom of this section before
treating any part of the phase as fully proven — it is not, in specific and stated ways,
and the property test's own findings (D-176, corrected as D-184) are the reason to trust
that statement rather than merely take it on faith.

- [x] `leave_type` **table built in P4** (D-127) — seeded via `manage.py seedleavetypes`,
      all ten codes; two shapes FLAGGED (`STUDY`, `COMPASSIONATE`) rather than settled
- [x] `leave_evidence_type` — built chunk 2 (D-173), scoped to the four sick-leave variants.
      No tenant field, no RLS — pure reference data, guarded by `no_delete()` since
      `leave_application` points at it. `max_consecutive_days_without_note` is read from a
      new `statutory_parameter` row (BCEA s23(1)), never a literal. One shape FLAGGED:
      `NO_NOTE`'s exact boundary against `SELF_CERTIFIED` is a per-application computation
- [x] `leave_cycle` — anchored to the CURRENT engagement's own start date (D-163, not the
      workbook's own unique — deviation recorded), lazy, idempotent, non-overlapping by
      EXCLUDE. Closed at TERMINATION itself as of chunk 2 (D-172), not lazily on re-hire.
      12-month annual, 36-month sick and 12-month family responsibility are all now
      generated and exercised (chunk 4)
- [x] `leave_transaction` append-only ledger — sign convention enforced by CHECK, trigger
      backs append-only, reversal-of-reversal refused (D-164, D-167). RECONCILED against
      sheet 02 in chunk 2 (D-170): `days`/`hours`, both nullable, exactly one populated —
      sheet 02's own column names, kept nullable against D-164's own settled decision.
      `leave_application` is now a real FK (D-175)
- [x] Accrual engine, THREE leave types through the SAME `run_monthly_accrual` entry point
      (chunk 4, task 2's own instruction — no parallel run mechanism): ANNUAL's monthly
      straight-line, per-17-days and per-17-hours methods (chunk 1); SICK's two-phase BCEA
      s22 accrual — an attendance-driven ratio in cycle 1's first six months, then ONE
      top-up transaction to the full six-week-equivalent LESS what was taken, proven as the
      TOTAL rather than the delta (D-181); FAMILY_RESPONSIBILITY's single upfront grant per
      cycle, no carry-over, never paid out (D-183). **Chunk 4b:** D-181's reasoning corrected
      (one entitlement, availability restricted for six months — arithmetic unchanged) and
      s22(4)'s "may" made the employer's election, `SICK_FIRST_CYCLE_REDUCTION`, default TRUE
      (D-186); the transition now nets reversed accruals, and SICK / FAMILY_RESPONSIBILITY
      cycles are always in days whatever an entitlement row's method says (both D-190, found
      by the property test); the family responsibility grant waits for s27(1) (D-189). A quantization defect shared by ANNUAL's
      own per-days/per-hours-worked methods and SICK's new ratio phase was found and fixed
      in the same pass (D-182). `AccrualNotSupportedError` still names the gap for
      MATERNITY, PARENTAL and ADOPTION — chunk 5's own work
- [x] `leave_accrual_run` — idempotent per employee per leave type per period, proven by
      running the engine twice and asserting the ledger unchanged, now for all three
      implemented leave types
- [x] `leave_application` + `leave_application_day` with part days and holidays inside a span
      — built chunk 2 (D-174). A public holiday or a rest day inside a span is never
      deducted; a half day deducts 0.500; an hourly employee's application deducts hours and
      never converts. An overdrawn application is never refused — the excess is capped
      against the ledger and falls to unpaid. Evidence gates pay, never the leave itself.
      `ANNUAL_UNAUTHORISED` (`balance_source='parent'`) now resolves to ANNUAL's own cycle
      and balance (chunk 4, D-180) — an unauthorised absence measurably costs the employee
      annual leave, proven by a dedicated test. **Chunk 4b:** `unpaid_hours` exists (D-188) —
      NOT NULL, `>= 0`, and the unpaid portion is held in the application's own unit, now for
      every reason a day is unpaid, not only the overdraw. Days-basis applications are NOT
      converted to hours (the brief's premise, refused — see D-188). STILL A LIMITATION: the
      overdraw falls unpaid a whole day at a time, so part of a day the balance could cover
      is unpaid too. A family responsibility application is REFUSED, naming the failing limb
      and its figure, unless BCEA s27(1)'s both limbs hold (D-189)
- [x] Employer authorisation; self-approval blocked, escalating to the owner — built chunk 2
      (D-174). Only the sole owner, with no other approver, may self-approve, and only with
      a mandatory reason visible in the leave register. Approval refuses a day already
      captured as worked (naming the date) or locked by a payroll run (naming the run), and
      writes the ledger transaction and the attendance day together. Cancelling REVERSES the
      ledger and removes the attendance days it wrote, even after the leave was taken
- [x] Forfeiture — NOT a job, BY DESIGN (D-165, Kobus's decision) — **and that half never
      will be ticked, because it is never going to be built**. What chunk 3 DOES build and
      tick: `leave/forfeiture.py::capture_forfeiture()`, a manual capture the employer
      triggers by hand, naming an employee, leave type, cycle and reason. Refuses a
      forfeiture larger than the cycle's balance (naming both figures) or against a cycle
      with no balance; reversible like any other ledger entry. Chunk 1's own
      `test_no_forfeiture_transaction_is_ever_written_automatically` is UNCHANGED and still
      passes — see D-177
- [x] The forfeiture deadline warning (task 3, not in the workbook's own table list — a
      query, not a table) — `leave/warnings.py::forfeiture_warnings()`. For an employer,
      every ANNUAL cycle that has ENDED with a balance still outstanding, bucketed
      approaching/due/past against `leave_rule_set.annual_leave_forfeit_months` (read
      through `statutory.resolve`, never a literal). Read-only — it writes nothing, exactly
      because nothing may forfeit automatically. Excludes a terminated employee's closed
      cycle, which is a BCEA s40(b) payout question, not a forfeiture one (D-178)
- [x] `public_holiday_observance` overrides (task 4, D-179) — built to sheet 02's own
      columns. **Changes chunk 2's own answer, deliberately**: an employer whose observance
      row says `is_observed=FALSE` has its employees work that date as ordinary, so it BECOMES
      a working day and IS deducted from leave, on the same calendar date a different
      employer's employees still get free. Proven both directions, same date, two employers,
      in one test. COMPLIANCE NOTE recorded in the model's own docstring and flagged for
      O-06: a row with `is_observed=FALSE` RECORDS an agreement under BCEA s18(3); it does
      not MAKE one, and nothing in this codebase checks that a genuine agreement stands
      behind a row before honouring it
- [x] The negative balance report (chunk 4, task 1, D-184) — `leave/negative_balances.py`.
      Same shape as the forfeiture warning: read-only, no job, no screen, writes nothing.
      Every OPEN cycle for an employer's employees, ANY leave type, currently below zero,
      with every ledger row that sums to it — because a negative balance is not a
      leave-type-specific statutory question the way forfeiture's grace period is. This is
      the corrected other half of D-176 (below): the false "never negative" invariant is
      replaced with the true one, and this query is what makes a negative balance a fact the
      employer is shown rather than a state the system quietly holds. **Recorded against
      P7, not implemented here (D-185): a termination payout must never net a negative
      balance off the final payment — recovering it is a BCEA s34 deduction requiring
      consent, and P7 must surface this query's own figure for a human decision, never
      subtract it automatically**

**Done when:** every employee's balance reconciles to their ledger in every scenario,
with no drift. **PROVEN for ANNUAL, SICK and FAMILY_RESPONSIBILITY; NOT for MATERNITY,
PARENTAL or ADOPTION, which have no accrual until chunk 5.** Reconciliation alone only proves
the balance equals what was posted; D-193 found two paths that posted a deduction for a day
nobody paid for. Both are closed by Kobus's decisions: an unauthorised absence is the
employer's election, unpaid and uncharged or annual leave charged and paid (D-195); sick
leave withheld for want of a certificate is unpaid and not charged (D-196). Each has a test
asserting the unpaid figure and the balance together.
`leave/tests/test_reconciliation_property.py` (Hypothesis) generates random sequences — up
to 16 actions, any of the three leave types, DAYS or HOURS, either s22(4) election — of
manual accruals, adjustments, applications approved and cancelled (full and part day),
forfeitures, reversals of any row, attendance captured through `capture()`, jumps of up to
40 months, and runs of the REAL accrual engine. After every step it confirms the balance
equals an independent ledger sum, the cache agrees, and any negative balance is reported by
`leave/negative_balances.py` with its rows; and it checks each engine row against its rule
(D-190). As of chunk 4b the ledger reconciles, and the engine's rows hold, across 400- and
1,000-example local runs with every engine path reached — **after** fixing the two defects
this extension found (D-190): a day entitlement written into an hours cycle, and a reversed
accrual still deducted from the six-month sick top-up. Neither was visible to reconciliation
alone: both left a ledger that summed.

**D-176, corrected as D-184 (chunk 4).** Chunk 3's original fix to what the property test
found — reversing an EARLIER transaction (say, an accrual) after a LATER adjustment or
forfeiture has already relied on its contribution being there, which can leave a cycle
showing a legitimate deficit — was to treat that sequence as outside the property test's
OWN definition of a valid one, i.e. to stop generating it. That was the wrong fix: it
preserved a false invariant ("no balance is ever negative") by removing the coverage that
disproved it, rather than accepting what the ledger was correctly telling it. In
production the sequence still runs, the balance still goes negative, and nothing told
anyone. Chunk 4 restores the generator (the skip is gone) and replaces the invariant with
the one that is actually true — see the negative balance report bullet above and D-184.
This is still a genuine, worthwhile finding from writing the property test in the first
place — not a defect in `leave/ledger.py`, which was never asked to (and should not)
understand causality between independent rows — only the response to the finding was
wrong the first time.

**What this proof does NOT cover, stated plainly rather than implied by a tick:**
- **CI runs 40 examples.** The rarer paths — the 36-month sick boundary, a transition after
  a reversed accrual — were each reached in well under 1% of examples at 400, so a single CI
  run exercises them only occasionally. The local runs are the evidence for them;
  `LEAVE_PROPERTY_EXAMPLES` raises the count.
- **`ANNUAL_UNAUTHORISED` is resolved** (chunk 4, D-180) and its unpaid hours are captured
  (D-188), but the property test does not generate it; `test_accrual_chunk4.py` and
  `test_applications.py` do.
- **`ACCOM_DED` is checked against the gazetted ceiling at capture (D-198), but a line is not
  re-checked when a later gazette lowers it**; that, and the s34 total, belong to P7. The cap is an
  explicit pair as of chunk 4d — capped boolean (no default) plus a nullable percentage — so an
  instrument that states no cap and one that forbids the deduction are no longer the same row.
- **Family responsibility ELIGIBILITY is PROVISIONAL for domestic workers** (D-189, O-06): SD7 cl
  21(1) is verified against the 2002 gazette only. If SD7 departs from s27(1) as it departs on the
  quantum, the code unlawfully refuses a domestic worker who works three days a week.
- **Part-day overdraw** — see `unpaid_hours` above: whole days only.
- **SD7 clause 21(1) is verified against the 2002 gazette, not a current consolidation**
  (D-194). No consolidated SD7 could be read; confirming no amendment since is queued for
  O-06. SD1 clause 22(1) is verified against the 1 March 2026 consolidation.
- **Termination payout — P7's job, with a constraint now recorded ahead of it (D-185).** A
  terminated employee's closed cycle balance is proven untouched at termination (chunk 2's
  own tests) but paying it out is not this phase's work, and when P7 does it, it must
  surface a negative balance for a human decision rather than net it off automatically.

---

## P7 — Payroll Engine · 8 weeks · 9 tables

The one that has to be right.

**Chunks 1 to 7 built** (19 September 2026, D-207 to D-235): the calculator contract, the
trace, UIF, SDL, PAYE, gross pay, leave pay, the termination payout, the payslip and
year-to-date tables, and the run lifecycle with its validation gate. **P2 verification has
still not happened — 0 of 21 versions — and that is now ENFORCED rather than described**: a
run refuses at approval, by the gate, with a message naming `verifystatutory`. What remains
before a payroll can actually run is that verification, and the payslip ASSEMBLY (chunk 8):
reading each employee's attendance, leave, remuneration and tax profile, resolving the
statutory rows and calling the calculators. `calculate()` refuses by name until then. **The assembly is BLOCKED and deliberately not started.** Reference data version
REF-2026.03.01 is loaded and reconciles but is NOT verified, so `in_force_on()` cannot see it
and no payroll run can start — periods, runs and payslips all read effective-dated rows.
Calculators are not blocked by that, because a pure function takes its statutory figures as
inputs, so they are what chunk 1 built. **P2 verification by a second person is the gate:
nothing below marked "assembly" can begin until Kobus runs `verifystatutory`.**

- [x] The calculator contract (D-207) — one frozen input, one frozen result, provenance as
      row KEYS, `Money` carrying exact and rounded, enforced by a source-reading import guard
      that is itself tested against violating snippets
- [x] `payroll_calculation_trace` — inputs, reference rows read, outputs, warnings (D-208).
      Tenant-scoped with `enable_rls()`, append-only by trigger, written even for a zero. The
      calculator produces the structure; `payroll.trace.record()` persists it.
      `payslip_id_ref` is a forward reference until the payslip exists
- [x] UIF with ceiling (D-210) — base is `min(remuneration, ceiling)` from s6(2)'s "as
      exceeds"; commission out, bonus in; the s4(1)(a) exemption declared, never computed
- [x] SDL with the human-set exemption flag (D-209) — forward-looking s4(b), so liability is
      a boolean input and there is nowhere to hand it a payroll history
- [ ] `pay_period` generation and lifecycle — ASSEMBLY, blocked on P2 verification
- [x] `payroll_run` state machine: draft → calculating → calculated → approved → finalised
      — CHUNK 7 (D-233). `LEGAL_TRANSITIONS` is data and `transition()` is the one place a
      status moves. Finalisation freezes the snapshots, locks the attendance, closes the
      period and rebuilds the year-to-date cache in one transaction; reversal is a new run
      with a mirrored payslip per payslip and the original untouched (D-235)
- [x] Gross pay calculators, one per pay basis — CHUNK 3 (D-216 to D-219). ONE calculator,
      because BCEA ss 10, 16 and 18 each state a TOTAL for the day: the premium is that total
      less what the basic already paid, and what the basic covers is a fact about the basis.
      s18 prices a DAY and not the hours in it, and s16(2) floors a short Sunday at a daily
      wage — both read from the Act, both different from what the multiplier columns suggest.
      Standby and the earnings-threshold exclusions REFUSE rather than guess (O-22, O-24)
- [x] PAYE: annual equivalent method, bonus annualised **once**, directives — CHUNK 2
      (D-212 to D-215). One formula with `periods_worked`, which carries 1 for an ordinary
      month, 7 for a mid-year leaver and SARS's own decimal portion 3 ÷ 7 for a part-period
      starter. Statutory rates, not the deduction tables — both are sanctioned and SARS's
      worked examples use the other one, so the golden file splits into exact reproductions
      (six cumulative band bases, three tax thresholds, the medical credit scale, SARS's own
      annual-equivalent arithmetic) and method reproductions of the two fully worked examples
- [ ] COIDA accumulation (UIF and SDL are done — see chunk 1 above)
- [x] Leave pay, including the variable-earnings average — CHUNK 4 (D-220 to D-223).
      Two rates and s35 decides which; the s35(4) trigger is DECLARED because "fluctuates
      significantly" has no statutory threshold, and the 13-week window is loaded reference
      data. The average is not floored at the contractual rate — s21(1)(b) makes s35 the
      calculation — and what counts as remuneration stays the component flag, held to
      Government Notice 691 by a test that pins the flagged set by name
- [x] `termination_payout` — notice, pro-rata leave, severance — CHUNK 5 (D-224 to
      D-227). The pro-rata BONUS is not built: it belongs with `annual_bonus_cycle` below,
      and SD1's is the only one gazetted. s40(c) turned out to be a FLOOR rather than a
      formula, SD1 clause 23(1)(d) turned out not to conflict with s38 at all, and BCEA
      s84(1) turned out not to be implemented anywhere (O-26)
- [ ] `annual_bonus_cycle` accruing monthly
- [x] `payslip`, `payslip_line` with SARS source codes and employee snapshot — CHUNK 6
      (D-228 to D-231). The TABLES, with every guard on them: invariant 4 by trigger,
      invariant 6 as a CHECK that the rounded amount IS the exact one rounded, invariant 7
      as frozen text beside the foreign keys. Nothing generates or finalises one — that is
      the run, and the run is blocked
- [x] `ytd_accumulator`, rebuildable from finalised payslips — CHUNK 6 (D-231). Keyed on
      the SARS source code, rebuilt from scratch every time with no incremental path
      (D-153's lesson), and checked by a ground truth that never reads the cache's own
      bookkeeping
- [x] `payroll_validation_issue` and the approval gate — CHUNK 7 (D-232, D-234). **The P2
      gate now refuses rather than being described**: nothing had ever called
      `in_force_on()` or read `data_current_through`, and the gate is where they are read.
      Issues are derived and rewritten; a resolution is keyed on the PROBLEM, not the row,
      so it survives re-validation — with a named person and a mandatory reason on it
- [ ] Golden-file tests against every published SARS and DEL worked example
- [ ] Property-based invariants: balance equals ledger, net never negative, UIF never over ceiling

**Done when:** SARS worked examples reproduce exactly and a finalised payslip is
reproducible from its trace alone.

---

## P8 — Statutory Outputs & Banking · 4 weeks · 9 tables

- [ ] Payslip PDF generation
- [ ] `secure_document_link` + OTP-gated delivery, 30-day device trust
- [ ] `bank_payment_file`, `bank_payment_line` — generic CSV plus the major bank formats
- [ ] `emp201_return`, `ui19_declaration`, `ui19_line`
- [ ] `irp5_certificate`, `irp5_line`, e@syFile-compatible export
- [ ] `coida_return_of_earnings` with per-employee capping
- [ ] Print remains a first-class delivery channel

**Done when:** a tax year closes and produces an IRP5 file that e@syFile imports without error.

---

## P9 — Discipline & Documents · 4 weeks · 6 tables

Can overlap P7 and P8.

- [ ] `disciplinary_case` with the full procedural trail
- [ ] `disciplinary_action` with validity windows; progressive-discipline check
- [ ] `disciplinary_evidence`
- [ ] `document_template` per sector and language
- [ ] `generated_document` with frozen merge data
- [ ] `document_signature` with hash, timestamp, IP and OTP evidence
- [ ] Document expiry alerts at 60, 30 and 7 days

**Done when:** a dismissal produces a complete, dated procedural pack in one click.

---

## P10 — Self-Service, Reporting & Console · 5 weeks · 4 tables

- [ ] Employee portal: payslips, IRP5, leave balances, leave applications, contact details
- [ ] Whitelist visibility — contract, payslips, IRP5 only
- [ ] Labourmax superuser console across all tenants
- [ ] Platform support role with time-limited, logged grants
- [ ] `notification` with all channels; `saved_report`; `system_announcement`
- [ ] Reports: headcount, leave liability, payroll summary, minimum-wage exceptions,
      EMP201 reconciliation, termination register, **cost per contract**

**Done when:** the superuser sees every tenant's status and no employer sees any data but
their own.

---

## P11 — Hardening, POPIA & Launch · 4 weeks · 2 tables

- [ ] Independent penetration test; findings closed before go-live
- [ ] `popia_consent`, `data_subject_request` with the statutory response clock
- [ ] Retention and purge jobs, with the five-year SARS carve-out encoded
- [ ] Operator agreement finalised with the lawyer (O-12); household exemption resolved (O-13)
- [ ] Information Officer registered with the Regulator
- [ ] Backup and restore rehearsal against a clean environment
- [ ] Load test: 500-employee run under two minutes
- [ ] Monitoring, error tracking, alerting
- [ ] Pilot employer parallel run, two to three months

**Done when:** a restore rehearsal succeeds and a pilot employer's payroll reconciles to
the cent.

---

## A milestone worth aiming at

**P0 through P7** is roughly 34 weeks and produces a system that can genuinely run a
compliant payroll. That is the point at which a pilot employer becomes possible, and it is
a better target than a feature-complete launch. Statutory outputs can be produced by hand
for one pilot employer for one tax year — they cannot be produced by hand at scale, which
is why P8 is not deferred further.

Find the pilot employer during P5. Finding them early is worth more than any feature.
