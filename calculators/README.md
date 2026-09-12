# calculators

**Pure functions only.** No ORM imports. No database access. No file I/O. No `datetime.now()`.

Every function takes a frozen input structure and returns a result structure. The caller
assembles inputs by reading effective-dated rows; the calculator only computes.

This is what makes the payroll engine testable against SARS worked examples without a
database, and what stops calculation logic leaking into views and models where it becomes
impossible to verify.

## Rules

- 100% branch coverage. CI fails below it.
- A calculator without a golden-file test — reproducing a published SARS or DEL worked
  example exactly — does not ship.
- `Decimal` everywhere. Rounding `ROUND_HALF_UP` to 2 places happens at the payslip line
  and nowhere else; intermediates keep 4–6 places.
- Every calculator returns its trace alongside its result: the inputs it used, the
  reference rows it read, the outputs. The caller persists that to
  `payroll_calculation_trace`.

## Arriving in P7

`gross_pay` (one per pay basis) · `paye` · `uif` · `sdl` · `leave_pay` ·
`termination_payout` · `annual_bonus` · `minimum_wage_check`
