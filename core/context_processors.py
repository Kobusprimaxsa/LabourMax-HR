"""What every page's header needs: who is signed in, for which employer account,
and whether to SHOW write controls. Showing is not permitting — ``core/web.py``
decides that per request, whatever the template drew."""

from core.web import can_write


def shell(request):
    membership = getattr(request, "membership", None)
    return {
        "tenant_name": membership.tenant.trading_name if membership is not None else "",
        "membership_role": membership.get_role_display() if membership is not None else "",
        "can_write": can_write(request),
    }
