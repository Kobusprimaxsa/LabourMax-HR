"""BCEA s27(1): who family responsibility leave applies to (D-189).

    This section applies to an employee - (a) who has been in employment with
    an employer for longer than four months; and (b) who works for at least
    four days a week for that employer.

BOTH limbs. The one a naive implementation misses is (b): an employee two
years in on three days a week does not qualify, however long the service.

**The figures are the rule set's** - ``family_resp_min_service_months`` and
``family_resp_min_days_per_week``, per sector, cited - and never literals.
What IS written here is how each figure compares, because that is the
statute's wording rather than a number: "longer than" four months, so exactly
four months does not qualify and the first eligible day is the day after;
"at least" four days, so four does. Both determinations carry the same two
limbs in the same words — SD1 clause 22(1) and SD7 clause 21(1), read from the
clause text and quoted in D-194; SD7 departs only on the quantum (five days).

**Service is the CURRENT engagement's** (``current_engagement``), never an
edited first row (D-103): a re-hire's service starts again, the same anchor
the leave cycles use (D-163). Days a week is the ``work_schedule`` in force on
the date asked about.

A question, not a refusal: ``family_responsibility_eligibility()`` returns
the reasons, and each caller decides what ineligibility means for it - an
application is refused naming them; the grant simply does not happen yet,
because not having reached four months is the ordinary state of every new
hire rather than an error.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class Eligibility:
    reasons: list[str] = field(default_factory=list)
    eligible_from: datetime.date | None = None  # the first day limb (a) holds

    @property
    def is_eligible(self) -> bool:
        return not self.reasons


def family_responsibility_eligibility(employee, on_date: datetime.date) -> Eligibility:
    """Every s27(1) limb this employee fails on ``on_date``, each naming its
    figure. No reasons means eligible. Caller pins the tenant."""
    from attendance import scheduling
    from employees.engagements import current_engagement
    from statutory import resolve

    rules = resolve.leave_rules(employee.employer.sector, on_date)
    reasons: list[str] = []

    engagement = current_engagement(employee)
    eligible_from = None
    if engagement is None:
        reasons.append(
            f"{employee} has no engagement, so BCEA s27(1)(a)'s service length cannot be read."
        )
    else:
        months = rules.family_resp_min_service_months
        # "longer than": exactly `months` months is not enough; the day after is.
        eligible_from = (
            engagement.start_date + relativedelta(months=months) + datetime.timedelta(days=1)
        )
        if on_date < eligible_from:
            reasons.append(
                f"{employee} has been employed since {engagement.start_date:%d %B %Y}, and "
                f"BCEA s27(1)(a) requires longer than {months} months "
                f"(leave_rule_set.family_resp_min_service_months) - eligible from "
                f"{eligible_from:%d %B %Y}."
            )

    minimum_days = rules.family_resp_min_days_per_week
    schedule = scheduling.current_schedule(employee, on_date)
    if schedule is None:
        reasons.append(
            f"{employee} has no work schedule in force on {on_date:%d %B %Y}, so BCEA "
            f"s27(1)(b)'s days a week cannot be read."
        )
    elif schedule.days_per_week < minimum_days:
        reasons.append(
            f"{employee} works {schedule.days_per_week.normalize():f} days a week, and BCEA "
            f"s27(1)(b) requires at least {minimum_days} "
            f"(leave_rule_set.family_resp_min_days_per_week)."
        )

    return Eligibility(reasons=reasons, eligible_from=eligible_from)
