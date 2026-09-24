"""``python manage.py seeddemo`` — FAKE data to sign in with on a development machine.

Everything this writes is invented: the people, the ID numbers (valid checksums,
belonging to nobody), the addresses, the rates. It exists so the screens can be
looked at and P5's ten-minute capture can be timed, and for nothing else.

* **Refused unless DEBUG is on.** Never production, never a copy of it.
* **Refused if it has already run** — it never touches rows it did not make.
* **Every address ends in RFC 2606's ``.invalid``**, the suffix this build already
  treats as a development identity (D-262), so nothing it creates can be mistaken
  for a person, or ever receive mail.
* **Through the real services**: ``engage()`` for every engagement and
  ``employees.remuneration.capture()`` for every rate, so the minimum-wage check
  runs exactly as it would for a real employer. Schedules and tax profiles are
  written directly, as no service for them exists yet.

It writes no attendance: the point is to capture a month by hand.

Two tenants and one owner of both, so signing in shows the account picker:

* **Demo Household** — domestic, one worker on a monthly salary, Monday to Friday;
  and a READ-ONLY member, to see the grid without write controls.
* **Demo Cleaning Co** — contract cleaning, twenty cleaners paid by the hour,
  Monday to Friday: P5's "a month for twenty employees".
"""

from __future__ import annotations

import datetime
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.managers import tenant_context
from core.models import AppUser, Tenant, TenantMembership
from employees.engagements import engage
from employees.identity import luhn_check_digit
from employees.models import Employee, EmployeeTaxProfile, WorkSchedule, WorkScheduleDay
from employees.remuneration import capture as capture_rate
from employers.models import Employer, PayGroup
from statutory.models import Sector, WorkingTimeRuleSet

OWNER = "owner@demo.labourmax.invalid"
VIEWER = "viewer@demo.labourmax.invalid"
HOUSEHOLD = "Demo Household"
CLEANERS = "Demo Cleaning Co"
ENGAGED = datetime.date(2026, 3, 2)

#: Invented names for invented people.
FIRST = [
    "Thandi",
    "Sipho",
    "Lerato",
    "Themba",
    "Naledi",
    "Bongani",
    "Zanele",
    "Mpho",
    "Ayanda",
    "Kagiso",
    "Nomsa",
    "Lwazi",
    "Palesa",
    "Tshepo",
    "Busi",
    "Sizwe",
    "Refilwe",
    "Andile",
    "Dineo",
    "Karabo",
]
LAST = [
    "Demo-Mokoena",
    "Demo-Dlamini",
    "Demo-Nkosi",
    "Demo-Khumalo",
    "Demo-Mahlangu",
    "Demo-Ndlovu",
    "Demo-Zulu",
    "Demo-Molefe",
    "Demo-Naidoo",
    "Demo-Pillay",
    "Demo-Botha",
    "Demo-van Wyk",
    "Demo-Maseko",
    "Demo-Sithole",
    "Demo-Mthembu",
    "Demo-Shabalala",
    "Demo-Radebe",
    "Demo-Cele",
    "Demo-Hadebe",
    "Demo-Mbatha",
]


class Command(BaseCommand):
    help = "Seed FAKE demo tenants to sign in with on a development machine. DEBUG only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            help="Password for both demo sign-ins. A random one is generated and printed "
            "if not given.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seeddemo writes FAKE data and runs only with DEBUG on.")
        if Tenant.objects.filter(trading_name__in=[HOUSEHOLD, CLEANERS]).exists():
            raise CommandError(
                "The demo tenants already exist. seeddemo never touches rows it did not "
                "just make; sign in with the password printed when it first ran."
            )
        if not WorkingTimeRuleSet.objects.exists():
            raise CommandError(
                "No working time rule set is loaded. Run `python manage.py loadstatutory "
                "--all` first — attendance cannot be bucketed without one."
            )

        password = options["password"] or secrets.token_urlsafe(12)
        today = datetime.date.today()
        self._numbers = iter(range(1, 999))

        # One transaction for the lot: a rate the minimum-wage check refuses
        # halfway through leaves NOTHING behind, rather than half a demo that the
        # "already exists" check would then refuse to finish.
        with transaction.atomic():
            owner = self._user(OWNER, password, "Demo", "Owner")
            viewer = self._user(VIEWER, password, "Demo", "Viewer")

            household = self._tenant(
                HOUSEHOLD, Sector.Code.DOMESTIC, [(owner, "owner"), (viewer, "read_only")]
            )
            self._staff(
                household,
                count=1,
                basis="monthly",
                rate=Decimal("6500.00"),
                today=today,
                start=datetime.time(8, 0),
                end=datetime.time(17, 0),
            )
            cleaners = self._tenant(CLEANERS, Sector.Code.CONTRACT_CLEANING, [(owner, "owner")])
            self._staff(
                cleaners,
                count=20,
                basis="hourly",
                rate=Decimal("35.00"),
                today=today,
                start=datetime.time(7, 0),
                end=datetime.time(16, 0),
            )

        self.stdout.write(self.style.SUCCESS("Demo data seeded. ALL OF IT IS FAKE."))
        self.stdout.write(f"  Sign in at /sign-in/ as {OWNER} (owner of both demo accounts)")
        self.stdout.write(f"                    or {VIEWER} (read-only, Demo Household)")
        self.stdout.write(f"  Password for both: {password}")
        self.stdout.write("  It is printed once and stored only as an Argon2id hash.")

    # ------------------------------------------------------------------ parts

    def _user(self, email, password, first, last):
        return AppUser.objects.create_user(
            email=email, password=password, first_name=first, last_name=last
        )

    def _tenant(self, name, sector_code, members):
        sector = Sector.objects.get(code=sector_code)
        tenant = Tenant.objects.create(trading_name=name, status=Tenant.Status.ACTIVE)
        # atomic FIRST (D-92): this runs in autocommit, and the pin is scoped to
        # the transaction it is set in.
        with transaction.atomic(), tenant_context(tenant.pk):
            employer = Employer.objects.create(tenant=tenant, trading_name=name, sector=sector)
            is_hourly = sector_code == Sector.Code.CONTRACT_CLEANING
            group = PayGroup.objects.create(
                tenant=tenant,
                employer=employer,
                name="Cleaners (hourly)" if is_hourly else "Monthly staff",
                pay_frequency=(
                    PayGroup.PayFrequency.HOURLY if is_hourly else PayGroup.PayFrequency.MONTHLY
                ),
                period_end_rule=PayGroup.PeriodEndRule.CALENDAR_MONTH_END,
                first_period_start=datetime.date(2026, 3, 1),
            )
            for user, role in members:
                TenantMembership.objects.create(tenant=tenant, user=user, role=role)
        self.stdout.write(f"  {name}: tenant, employer, pay group '{group.name}'")
        return tenant, employer, group

    def _staff(self, where, *, count, basis, rate, today, start, end):
        tenant, employer, group = where
        for index in range(count):
            n = next(self._numbers)
            body = f"900101{5000 + n:04d}08"[:12]
            with transaction.atomic(), tenant_context(tenant.pk):
                employee = Employee.objects.create(
                    tenant=tenant,
                    employer=employer,
                    first_name=FIRST[index % len(FIRST)],
                    last_name=LAST[index % len(LAST)],
                    date_of_birth=datetime.date(1990, 1, 1),
                    mobile_number=f"+2780000{n:04d}",
                    email=f"worker{n}@demo.labourmax.invalid",
                    id_number=body[:-1] + str(luhn_check_digit(body[:-1])),
                )
            engage(
                employee,
                start_date=ENGAGED,
                job_title="Cleaner" if basis == "hourly" else "Domestic worker",
            )
            with transaction.atomic(), tenant_context(tenant.pk):
                schedule = WorkSchedule.objects.create(
                    tenant=tenant,
                    employee=employee,
                    days_per_week=Decimal("5"),
                    ordinary_hours_per_week=Decimal("40"),
                    effective_from=ENGAGED,
                )
                for cycle_day in range(7):
                    working = cycle_day < 5
                    WorkScheduleDay.objects.create(
                        tenant=tenant,
                        work_schedule=schedule,
                        cycle_day=cycle_day,
                        is_working_day=working,
                        ordinary_hours=Decimal("8") if working else Decimal("0"),
                        start_time=start if working else None,
                        end_time=end if working else None,
                        unpaid_break_minutes=60 if working else 0,
                    )
                EmployeeTaxProfile.objects.create(
                    tenant=tenant,
                    employee=employee,
                    tax_status=EmployeeTaxProfile.TaxStatus.STANDARD,
                    effective_from=ENGAGED,
                )
            # The real service: the minimum wage is checked exactly as for a real
            # employer, and a rate below it is refused, not acknowledged.
            capture_rate(
                employee,
                pay_basis=basis,
                rate_amount=rate,
                effective_from=ENGAGED,
                pay_group=group,
                hours_per_day=Decimal("8"),
                days_per_week=Decimal("5"),
                hours_per_week=Decimal("40"),
                as_at=today,
            )
        self.stdout.write(f"    {count} employee(s), {basis} at {rate}")
