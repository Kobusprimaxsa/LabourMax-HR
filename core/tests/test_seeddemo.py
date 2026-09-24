"""``seeddemo``: fake data, DEBUG only, through the real services, and usable.

``transaction=True`` because a management command runs in autocommit, where a
tenant pin set outside a transaction is gone before the write (D-92) — the
trap ``seedcomponents`` shipped with. The ordinary test wrapper would hide it.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import Client
from django.urls import reverse

from core.managers import tenant_context
from core.models import AppUser, Tenant
from employees.models import Employee, EmployeeRemuneration
from statutory.loader import load_reference_data

pytestmark = pytest.mark.django_db(transaction=True)

REFERENCE = pathlib.Path(__file__).resolve().parents[2] / "reference"
NEEDED = (
    "ref-2026.03.01.json",
    "ref-2026.03.01-holidays.json",
    "ref-2026.03.01-rules.json",
    "ref-2026.03.01-sd1.json",
    "ref-2026.03.01-employment.json",
    "ref-2026.03.01-remuneration.json",
)


@pytest.fixture
def reference():
    for name in NEEDED:
        load_reference_data(json.loads((REFERENCE / name).read_text(encoding="utf-8")))


def seed(**options):
    call_command("seeddemo", password="demo-password-123", **options)


def test_it_seeds_two_fake_tenants_through_the_real_services(reference, settings):
    settings.DEBUG = True
    seed()

    tenants = {t.trading_name: t for t in Tenant.objects.filter(trading_name__startswith="Demo")}
    assert set(tenants) == {"Demo Household", "Demo Cleaning Co"}
    for tenant, staff, basis in (
        (tenants["Demo Household"], 1, "monthly"),
        (tenants["Demo Cleaning Co"], 20, "hourly"),
    ):
        with transaction.atomic(), tenant_context(tenant.pk):
            assert Employee.objects.count() == staff
            rates = EmployeeRemuneration.objects.all()
            assert {r.pay_basis for r in rates} == {basis}
            assert all(r.minimum_wage_rate_id is not None for r in rates), "the floor was read"
            assert not any(r.is_below_minimum for r in rates)
            assert all(e.email.endswith(".invalid") for e in Employee.objects.all())

    assert {u.email for u in AppUser.objects.all()} == {
        "owner@demo.labourmax.invalid",
        "viewer@demo.labourmax.invalid",
    }


def test_the_owner_signs_in_to_the_picker_and_reaches_a_grid(reference, settings):
    settings.DEBUG = True
    seed()
    client = Client()

    signed_in = client.post(
        reverse("login"),
        {"email": "owner@demo.labourmax.invalid", "password": "demo-password-123"},
    )
    assert signed_in["Location"] == reverse("choose_tenant")

    cleaners = Tenant.objects.get(trading_name="Demo Cleaning Co")
    client.post(reverse("choose_tenant"), {"tenant": str(cleaners.public_uid)})
    home = client.get(reverse("home")).content.decode()
    assert "Cleaners (hourly)" in home

    grid_url = home.split('href="/attendance/', 1)[1].split('"', 1)[0]
    grid = client.get(f"/attendance/{grid_url}").content.decode()
    assert grid.count("Demo-") >= 20, "all twenty cleaners are on the grid"


def test_it_refuses_without_debug(reference, settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="DEBUG"):
        seed()
    assert not Tenant.objects.exists()


def test_it_refuses_to_run_twice(reference, settings):
    settings.DEBUG = True
    seed()
    with pytest.raises(CommandError, match="already exist"):
        seed()
    assert Tenant.objects.count() == 2


def test_it_refuses_with_no_rule_set_loaded(settings):
    settings.DEBUG = True
    with pytest.raises(CommandError, match="loadstatutory"):
        seed()


def test_a_rate_below_the_minimum_leaves_nothing_behind(reference, settings, monkeypatch):
    """PROVE EVERY GUARD FAILS: one transaction for the lot. Force the cleaners'
    rate under the floor and the minimum-wage check refuses — and the household
    seeded before it is rolled back with it."""
    from core.management.commands import seeddemo
    from employees.remuneration import BelowMinimumWageError

    original = seeddemo.Command._staff

    def cheap(self, where, **kwargs):
        if kwargs["basis"] == "hourly":
            kwargs["rate"] = Decimal("1.00")
        return original(self, where, **kwargs)

    monkeypatch.setattr(seeddemo.Command, "_staff", cheap)
    settings.DEBUG = True

    with pytest.raises(BelowMinimumWageError):
        seed()
    assert not Tenant.objects.exists()
    assert not AppUser.objects.exists()
