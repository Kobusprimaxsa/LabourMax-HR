"""A URL configuration used only by the request-context tests.

The probe answers, from inside a real request, the two questions a view that
has lost its tenant cannot tell from an empty table: what does the DATABASE
session hold, and how many rows of a tenant table can it see.
"""

from django.db import connection
from django.http import JsonResponse
from django.urls import path

from employers.models import Employer


def probe(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('labourmax.tenant_id', true)")
        (pinned,) = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM employer")
        (visible_by_sql,) = cursor.fetchone()
    return JsonResponse(
        {
            "pinned": pinned or "",
            "request_tenant_id": getattr(request, "tenant_id", None),
            "employers_by_sql": visible_by_sql,
            "employers_by_orm": Employer.objects.count(),
        }
    )


urlpatterns = [path("probe/", probe)]
