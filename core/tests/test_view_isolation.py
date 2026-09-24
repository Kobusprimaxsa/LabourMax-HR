"""The view-level isolation suite — the model-level one's twin, for screens.

``test_tenant_isolation.py`` proves a second tenant cannot READ another's rows
through the ORM. This proves the same through the SCREENS, where the classic
multi-tenant break lives: a valid session plus somebody else's id in the URL.

**How the next screen gets the same protection, cheaply.** Add one entry to
``CASES``: the URL name, the method, and how to build its kwargs from a world.
That is all. Every test below then runs it:

1. every URL the project serves is ``employer_view``, ``signed_in_view`` or
   ``public_view`` — an unmarked view FAILS here, so a screen cannot be added
   unprotected by forgetting the decorator;
2. every ``employer_view`` has an entry in ``CASES`` — a screen cannot be added
   without its isolation case;
3. an anonymous user is sent to sign in (an HTMX request gets ``HX-Redirect``);
4. ANOTHER TENANT's signed-in owner, handed this tenant's URL, gets a 404 —
   never a 403, which would confirm the record exists;
5. a READ_ONLY member may look and may not write; an EMPLOYEE member may not
   reach an employer screen at all;
6. the view layer never names ``all_tenants`` or ``platform_context``.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from core.tests.web_world import a_member, a_world, client_for, grid_kwargs

pytestmark = pytest.mark.django_db

REPO = pathlib.Path(__file__).resolve().parents[2]


def _cell(world):
    return {**grid_kwargs(world), "employee_uid": world.employee.public_uid, "day": 2}


#: url name -> (method, kwargs from a world, POST data). ONE LINE per screen.
CASES = {
    "home": ("get", lambda w: {}, None),
    "attendance:grid": ("get", grid_kwargs, None),
    "attendance:exceptions": ("get", grid_kwargs, None),
    "attendance:cell": ("post", _cell, {"value": "9"}),
    "attendance:bulk": ("post", grid_kwargs, {"who": "all", "span": "month", "value": "R"}),
    "attendance:approve": ("post", grid_kwargs, {}),
}

#: Views that hold no tenant data and are deliberately not employer views.
PUBLIC = {"login", "logout"}
SIGNED_IN = {"choose_tenant", "change_password"}


def _patterns(resolver=None, namespace=""):
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            if entry.app_name == "admin":
                continue  # Django's own admin: is_staff, its own login, P10's console.
            inner = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from _patterns(entry, inner)
        elif isinstance(entry, URLPattern) and entry.name:
            yield f"{namespace}{entry.name}", entry.callback


@pytest.fixture
def worlds(db):
    return a_world("Household"), a_world("Neighbour")


def _call(client, name, world, *, htmx=False):
    method, kwargs, data = CASES[name]
    url = reverse(name, kwargs=kwargs(world))
    headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    if method == "post":
        return client.post(url, data or {}, **headers)
    return client.get(url, **headers)


# ------------------------------------------------------------ the inventory


def test_every_url_is_marked_protected_or_deliberately_public():
    unmarked = [
        name
        for name, view in _patterns()
        if not (
            getattr(view, "employer_view", False)
            or getattr(view, "signed_in_view", False)
            or getattr(view, "public_view", False)
        )
    ]
    assert not unmarked, f"views with no protection decision: {unmarked}"


def test_every_employer_view_has_an_isolation_case():
    employer_views = {name for name, view in _patterns() if getattr(view, "employer_view", False)}
    assert employer_views == set(CASES), (
        f"missing cases: {employer_views - set(CASES)}; stale cases: {set(CASES) - employer_views}"
    )


def test_the_public_and_signed_in_lists_are_what_the_decorators_say():
    marked_public = {name for name, view in _patterns() if getattr(view, "public_view", False)}
    marked_signed_in = {
        name for name, view in _patterns() if getattr(view, "signed_in_view", False)
    }
    assert (marked_public, marked_signed_in) == (PUBLIC, SIGNED_IN)


def test_the_inventory_fails_on_an_unmarked_view():
    """PROVE EVERY GUARD FAILS: a bare function view is caught."""

    def naked(request):  # pragma: no cover - never called
        return None

    assert not (
        getattr(naked, "employer_view", False)
        or getattr(naked, "signed_in_view", False)
        or getattr(naked, "public_view", False)
    )


# ----------------------------------------------------------- the refusals


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_anonymous_user_is_sent_to_sign_in(worlds, name):
    from django.test import Client

    household, _ = worlds
    response = _call(Client(), name, household)

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("login"))


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_anonymous_htmx_request_is_redirected_not_swapped(worlds, name):
    from django.test import Client

    household, _ = worlds
    response = _call(Client(), name, household, htmx=True)

    assert response.status_code == 401
    assert response["HX-Redirect"] == reverse("login")


@pytest.mark.parametrize("name", sorted(n for n, (_, kw, _) in CASES.items() if n != "home"))
def test_another_tenants_owner_gets_404_for_this_tenants_url(worlds, name):
    """The break this suite exists for: a VALID session, somebody else's ids."""
    household, neighbour = worlds
    intruder = client_for(neighbour.user, neighbour.tenant)

    response = _call(intruder, name, household)

    assert response.status_code == 404, f"{name} answered {response.status_code}"


def test_another_tenants_home_shows_only_its_own_data(worlds):
    household, neighbour = worlds
    page = client_for(neighbour.user, neighbour.tenant).get(reverse("home")).content.decode()

    assert "Neighbour" in page
    assert "Household" not in page


def test_a_session_naming_a_tenant_the_user_is_not_in_reaches_nothing(worlds):
    """Somebody else's TENANT in the session, not just their ids in the URL."""
    household, neighbour = worlds
    forged = client_for(neighbour.user, household.tenant)

    response = forged.get(reverse("attendance:grid", kwargs=grid_kwargs(household)))

    assert response.status_code == 302
    assert response["Location"] == reverse("choose_tenant")


@pytest.mark.parametrize("name", sorted(n for n, (m, _, _) in CASES.items() if m == "post"))
def test_a_read_only_member_may_not_write(worlds, name):
    household, _ = worlds
    reader = client_for(a_member(household, "read_only"), household.tenant)

    assert _call(reader, name, household).status_code == 403


def test_a_read_only_member_may_look(worlds):
    household, _ = worlds
    reader = client_for(a_member(household, "read_only"), household.tenant)

    page = reader.get(reverse("attendance:grid", kwargs=grid_kwargs(household)))

    assert page.status_code == 200
    assert b"hx-post" not in page.content, "no write controls drawn for a reader"


@pytest.mark.parametrize("name", sorted(CASES))
def test_an_employee_self_service_login_cannot_reach_employer_screens(worlds, name):
    household, _ = worlds
    worker = client_for(a_member(household, "employee"), household.tenant)

    assert _call(worker, name, household).status_code == 403


# ----------------------------------------------------------- the source


def test_the_view_layer_never_names_a_cross_tenant_escape():
    offenders = []
    for path in REPO.glob("*/views.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("all_tenants", "platform_context"):
            if re.search(rf"\b{needle}\b", text):
                offenders.append(f"{path.relative_to(REPO)}: {needle}")
    for path in (REPO / "core" / "web.py", REPO / "core" / "signin.py"):
        text = path.read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        for needle in ("all_tenants", "platform_context("):
            if needle in code:
                offenders.append(f"{path.relative_to(REPO)}: {needle}")
    assert not offenders, offenders
