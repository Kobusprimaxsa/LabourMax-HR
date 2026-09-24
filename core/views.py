"""Sign-in, sign-out, choosing a tenant, changing a password, and the home page.

The only public views are sign-in and sign-out (``public_view``). The picker and
the password change need a signed-in user but no tenant (``signed_in_view``).
Everything that shows tenant data is an ``employer_view`` (``core/web.py``).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core import signin
from core.web import employer_view, public_view, signed_in_view
from employers.models import Employer, PayGroup

SIGN_IN_REFUSED = "That email address and password do not match an account that can sign in."


@public_view
@require_http_methods(["GET", "POST"])
def sign_in(request):
    if request.user.is_authenticated and request.method == "GET":
        return redirect("home")
    email = ""
    if request.method == "POST":
        email = request.POST.get("email", "")
        attempt = signin.check_password(request, email, request.POST.get("password", ""))
        if attempt.succeeded:
            # An OTP step, when P1 builds it, goes here: park the user, send
            # the code, and call complete_sign_in() once it verifies.
            memberships = signin.complete_sign_in(request, attempt.user)
            if len(memberships) == 1:
                return redirect(_safe_next(request) or "home")
            return redirect("choose_tenant")
        if attempt.outcome == signin.Outcome.LOCKED:
            messages.error(
                request,
                "Too many failed attempts. The account is locked for a while; try again later.",
            )
        else:
            messages.error(request, SIGN_IN_REFUSED)
    return render(request, "core/sign_in.html", {"email": email})


def _safe_next(request) -> str | None:
    """``?next=`` only if it stays on this site — otherwise an open redirect."""
    wanted = request.GET.get("next") or ""
    if wanted and url_has_allowed_host_and_scheme(
        wanted, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return wanted
    return None


@public_view
@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@signed_in_view
@require_http_methods(["GET", "POST"])
def choose_tenant(request):
    memberships = signin.memberships_of(request.user)
    if request.method == "POST":
        wanted = request.POST.get("tenant", "")
        match = next((m for m in memberships if str(m.tenant.public_uid) == wanted), None)
        # The id arrives from the browser; it is honoured only if it names a
        # tenant this user has a live membership in, and choose() checks again.
        if match is not None and signin.choose(request, request.user, match.tenant_id):
            return redirect("home")
        messages.error(request, "Choose one of the accounts listed.")
    if not memberships:
        messages.info(request, "This sign-in is not linked to any employer account yet.")
    return render(request, "core/choose_tenant.html", {"memberships": memberships})


@signed_in_view
@require_http_methods(["GET", "POST"])
def change_password(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.password_changed_at = timezone.now()
        user.save(update_fields=["password_changed_at", "updated_at"])
        update_session_auth_hash(request, user)
        messages.success(request, "Password changed.")
        return redirect("home")
    return render(request, "core/change_password.html", {"form": form})


@employer_view
def home(request):
    employers = Employer.objects.filter(is_active=True).order_by("trading_name")
    groups = PayGroup.objects.filter(employer__in=employers, is_active=True).order_by(
        "employer__trading_name", "name"
    )
    return render(
        request,
        "core/home.html",
        {"employers": employers, "groups": groups, "month": timezone.localdate()},
    )
