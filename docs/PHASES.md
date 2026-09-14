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
- [ ] Contract cleaning Area C (KwaZulu-Natal): the BCCCI collective agreement (D-63)
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
- [ ] Monthly capture grid — sticky headers, keyboard navigation, colour-coded day types
- [ ] Pre-fill from schedule for salaried bases **only**; hourly and daily open blank
- [ ] Bulk actions filling empty cells only
- [ ] `attendance_import_batch` — preview, validate, apply, reverse (chunk 3)
- [ ] `timesheet_summary` aggregation with staleness flag
- [ ] Live exception panel: daily and weekly overtime caps, consecutive sick days, weekly rest.
      The pure calculation (`calculators.attendance.evaluate_exceptions`) exists for the
      first two and for the meal-interval and rest checks sheet 03 also asks for;
      consecutive sick days are P6 (a leave application to check against) and are not
      stubbed. The screen itself does not exist yet

**Done when:** a month for twenty employees is captured in under ten minutes, and an
uncaptured attendance-driven day blocks the payroll run.

---

## P6 — Leave Management · 5 weeks · 8 tables

- [ ] `leave_type` **table built in P4** (D-127) — P6 seeds it with both annual variants
      and all four sick-leave evidence types
- [ ] `leave_evidence_type`
- [ ] `leave_cycle` — 12-month annual, 36-month sick, anchored to engagement anniversary
- [ ] `leave_transaction` append-only ledger
- [ ] Accrual engine: monthly straight-line, per 17 days, per 17 hours, first-six-months sick
- [ ] `leave_accrual_run` — idempotent per employee per month
- [ ] `leave_application` + `leave_application_day` with part days and holidays inside a span
- [ ] Employer authorisation; self-approval blocked, escalating to the owner
- [ ] Forfeiture job writing explicit transactions
- [ ] `public_holiday_observance` overrides

**Done when:** every employee's balance reconciles to their ledger in every scenario,
with no drift.

---

## P7 — Payroll Engine · 8 weeks · 9 tables

The one that has to be right.

- [ ] `pay_period` generation and lifecycle
- [ ] `payroll_run` state machine: draft → calculating → calculated → approved → finalised
- [ ] Gross pay calculators, one per pay basis
- [ ] PAYE: annual equivalent method, bonus annualised **once**, directives
- [ ] UIF with ceiling; SDL with the human-set exemption flag; COIDA accumulation
- [ ] Leave pay, including the variable-earnings average
- [ ] `termination_payout` — notice, pro-rata leave, severance, pro-rata bonus
- [ ] `annual_bonus_cycle` accruing monthly
- [ ] `payslip`, `payslip_line` with SARS source codes and employee snapshot
- [ ] `payroll_calculation_trace` — inputs, reference rows read, outputs
- [ ] `ytd_accumulator`, rebuildable from finalised payslips
- [ ] `payroll_validation_issue` and the approval gate
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
