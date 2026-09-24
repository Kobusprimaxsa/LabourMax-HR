"""The view layer's security boundary. Every employer screen goes through here.

Written before the first screen, because the decisions made here are the ones
every screen in P8 to P10 inherits (D-296):

* ``employer_view`` is the ONLY way a view reaches tenant data. It refuses an
  anonymous user (a redirect to sign-in; for an HTMX request, an ``HX-Redirect``
  header, so a login page is never swapped into a grid cell), a user with no
  live membership in the session's tenant (the middleware has already declined
  to pin one), and a role that may not do what the method asks. It then
  asserts, against the DATABASE, that the tenant really is pinned for this
  request — the check that would have caught D-295 on the first page load.
* **Permissions are decided per request, on the server.** OWNER and ADMIN may
  write; READ_ONLY may look; EMPLOYEE (self-service) may not reach an employer
  screen at all. Any method that is not GET or HEAD is a write. A hidden button
  is not a permission, and none of this reads what the template chose to show.
* ``tenant_object`` resolves a record from a URL through the tenant-scoped
  manager, and a record in another tenant is a 404, never a 403 — a 403 would
  confirm it exists. Nothing here reads a tenant, employer or employee id from
  a form or a hidden field; ids come from the URL and are resolved inside the
  pinned tenant, so another tenant's id simply does not resolve.
* ``all_tenants`` and ``platform_context()`` appear nowhere in the view layer.
  ``core/tests/test_view_isolation.py`` scans for them.

``core/tests/test_view_isolation.py`` also enumerates every URL the project
serves and refuses one that is neither behind ``employer_view`` nor on the
short list of public views, so a new screen cannot be added unprotected — and
it runs every protected screen as an anonymous user and as another tenant's
user.
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import connection
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from core.models import TenantMembership

Role = TenantMembership.Role

#: Roles that may see employer screens at all.
EMPLOYER_ROLES = frozenset({Role.OWNER, Role.ADMIN, Role.READ_ONLY})
#: Roles that may change anything.
WRITE_ROLES = frozenset({Role.OWNER, Role.ADMIN})
SAFE_METHODS = frozenset({"GET", "HEAD"})


def is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _to_sign_in(request):
    if is_htmx(request):
        # HTMX would otherwise follow the redirect and swap the whole sign-in
        # page into whatever element asked — a grid cell, say.
        return HttpResponse(status=401, headers={"HX-Redirect": reverse("login")})
    return redirect_to_login(request.get_full_path())


def assert_tenant_pinned(request) -> None:
    """The DATABASE holds the tenant this request is for. A view that ran with
    the context lost would otherwise render an empty page rather than fail
    (D-295) — so it fails, loudly."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('labourmax.tenant_id', true)")
        (pinned,) = cursor.fetchone()
    if pinned != str(request.tenant_id):
        raise ImproperlyConfigured(
            f"This request is for tenant {request.tenant_id} and the database session "
            f"holds {pinned!r}. Row-level security would show an empty page; refusing "
            f"instead. TenantContextMiddleware must run, and must pin inside the "
            f"transaction the view runs in."
        )


def employer_view(view):
    """Protect an employer screen: signed in, a live membership, a role that
    may do what the method asks, and the tenant verifiably pinned."""

    @wraps(view)
    def protected(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _to_sign_in(request)
        membership = getattr(request, "membership", None)
        if membership is None:
            if is_htmx(request):
                return HttpResponse(status=401, headers={"HX-Redirect": reverse("choose_tenant")})
            return redirect("choose_tenant")
        if membership.role not in EMPLOYER_ROLES:
            raise PermissionDenied("Employer screens are not open to this account.")
        if request.method not in SAFE_METHODS and membership.role not in WRITE_ROLES:
            raise PermissionDenied("This account may look but not change anything.")
        assert_tenant_pinned(request)
        return view(request, *args, **kwargs)

    protected.employer_view = True
    return protected


def public_view(view):
    """Mark a view that is deliberately open — sign-in, sign-out. The isolation
    suite refuses any URL that is neither this nor ``employer_view``."""
    view.public_view = True
    return view


def signed_in_view(view):
    """Signed in, but before a tenant is chosen: the tenant picker and the
    password change. Touches no tenant data."""

    @wraps(view)
    def protected(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _to_sign_in(request)
        return view(request, *args, **kwargs)

    protected.signed_in_view = True
    return protected


def tenant_object(queryset, **lookup):
    """One record from the URL, through the tenant-scoped manager, or 404.

    Another tenant's record is invisible to both the manager and the policy,
    so it is indistinguishable here from one that never existed — which is
    exactly what the user should be told.
    """
    found = queryset.filter(**lookup).first()
    if found is None:
        raise Http404
    return found


def can_write(request) -> bool:
    """For the TEMPLATE only — what to show. Never what to allow."""
    membership = getattr(request, "membership", None)
    return membership is not None and membership.role in WRITE_ROLES
