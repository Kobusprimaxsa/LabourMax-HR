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

- [ ] Virtualenv, dependencies, local PostgreSQL, `.env` from `.env.example`
- [ ] Django project boots, `manage.py check` clean
- [ ] `TenantScopedModel`, `AuditMixin`, `TimestampedModel` base classes
- [ ] `TenantScopedManager` + `all_tenants` escape hatch + `tenant_context()`
- [ ] `TenantContextMiddleware` pinning the tenant to request **and** database session
- [ ] `core/db/rls.py` helpers; every tenant-scoped migration calls `enable_rls()`
- [ ] Models: `platform_setting`, `tenant`, `app_user`, `tenant_membership`,
      `user_invitation`, `tenant_ownership_transfer`, `otp_challenge`, `login_audit`,
      `audit_log`, `file_object`, `background_job`
- [ ] Custom user model wired as `AUTH_USER_MODEL`, Argon2id hashing
- [ ] Max-two-admin-users rule: column default, database trigger, application check
- [ ] Audit log signal wiring — field-level diffs, masked sensitive fields
- [ ] File storage abstraction with virus-scan status gate
- [ ] Generated tenant isolation suite passing
- [ ] CI green: lint, migration check, isolation suite, full tests

**Done when:** an automated cross-tenant leakage suite runs on every commit and passes,
and RLS is enabled *and forced* on every tenant-scoped table.

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

- [ ] `sector`, `sector_area`, `municipality_area_map`, `job_grade`
- [ ] `minimum_wage_rate` with exclusion constraints on overlapping ranges
- [ ] `tax_year`, `paye_tax_bracket`, `paye_rebate`, `medical_tax_credit_rate`
- [ ] `statutory_parameter` — UIF, SDL, COIDA, BCEA threshold, VAT
- [ ] `leave_rule_set`, `working_time_rule_set`, `termination_rule_set` per sector
- [ ] `public_holiday` with the Sunday-shift rule
- [ ] `sars_source_code`, `bank`, `bank_branch`
- [ ] `reference_data_version` with verification, golden-test flag, `data_current_through`
- [ ] `statutory_watch_item` seeded with the maintenance calendar
- [ ] Loader: staged import, gazette reference and source URL **mandatory** on every row
- [ ] Staleness guard blocking payroll runs beyond `data_current_through`
- [ ] Seed all values from workbook sheet 05

**Done when:** every statutory number lives in a table with a gazette citation, and
`grep -r` finds no hard-coded rate anywhere in the codebase.

---

## P3 — Employer Setup · 2 weeks · 7 tables

- [ ] `employer`, `employer_statutory_registration`, `employer_bank_account`
- [ ] `workplace` with client name, contract reference, area resolution
- [ ] `pay_group` and the pay period generator, all five frequencies
- [ ] `employer_setting`, seeded from sector at onboarding (including default sort)
- [ ] `payroll_component` catalogue, system components seeded and locked

**Done when:** an employer completes onboarding and generates a full year of pay periods.

---

## P4 — Employee Master File · 5 weeks · 15 tables

- [ ] `employee` with encrypted ID number, hash-based duplicate prevention, sort columns
- [ ] ICU collation set in the creating migration
- [ ] `employee_address`, `employee_contact`
- [ ] `employee_engagement` — start, termination, fixed-term end; re-hire support
- [ ] `employee_position` with site assignment (single or multi)
- [ ] `employee_remuneration` — five pay bases, derived rates computed once and stored
- [ ] Minimum-wage validation at capture
- [ ] `employee_bank_account` with active and end dates
- [ ] `employee_tax_profile`, `work_schedule`, `work_schedule_day`
- [ ] `employee_leave_entitlement`, `employee_recurring_component`, `employee_note`
- [ ] `document`, `document_category` — four-way attachment arc, visibility whitelist
- [ ] Current-state cache columns, maintained on write **and** by a nightly job
- [ ] Employee list: grouped by pay group, sector-derived default sort, remembered per user

**Done when:** capturing an employee below the sectoral minimum raises a visible, logged
exception, and a future-dated increase flips the cache on its own effective date.

---

## P5 — Attendance & Time · 4 weeks · 3 tables

- [ ] `attendance_day` with hour bucketing: ordinary, overtime, Sunday, public holiday, night
- [ ] Monthly capture grid — sticky headers, keyboard navigation, colour-coded day types
- [ ] Pre-fill from schedule for salaried bases **only**; hourly and daily open blank
- [ ] Bulk actions filling empty cells only
- [ ] `attendance_import_batch` — preview, validate, apply, reverse
- [ ] `timesheet_summary` aggregation with staleness flag
- [ ] Live exception panel: daily and weekly overtime caps, consecutive sick days, weekly rest

**Done when:** a month for twenty employees is captured in under ten minutes, and an
uncaptured attendance-driven day blocks the payroll run.

---

## P6 — Leave Management · 5 weeks · 8 tables

- [ ] `leave_type` seeded with both annual variants and all four sick-leave evidence types
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
