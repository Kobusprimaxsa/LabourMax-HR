"""Re-apply every row-level security policy. Two corrections.

1. ``''::bigint`` raises rather than evaluating false, and SQL gives no
   left-to-right evaluation guarantee, so the guard in the original policy did
   not reliably protect the cast. Now ``nullif(..., '')::bigint``, which yields
   NULL and therefore no rows.

2. The nullable-tenant policy no longer distinguishes "writable but not
   readable". Django appends ``RETURNING id`` to every INSERT and PostgreSQL
   applies the USING clause to rows returned that way, so a row the USING clause
   rejects cannot be inserted at all. See ``enable_rls_optional``.

Both were found by the isolation suite on its first run against a real database
— the first as "invalid input syntax for type bigint", the second as an
unexplained policy violation on a perfectly legitimate insert.

``enable_rls`` and ``enable_rls_optional`` both DROP POLICY IF EXISTS first, so
re-running them is idempotent. This is the pattern for every future policy
change: a new migration that re-applies, never an edit to an applied one.
"""

from django.db import migrations

from core.db.rls import disable_rls, enable_rls, enable_rls_optional

STRICT = [
    "tenant_membership",
    "user_invitation",
    "tenant_ownership_transfer",
    "file_object",
]

OPTIONAL = ["otp_challenge", "login_audit", "audit_log", "background_job"]


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = (
        [migrations.RunSQL(enable_rls(t), disable_rls(t)) for t in STRICT]
        + [migrations.RunSQL(enable_rls_optional(t), disable_rls(t)) for t in OPTIONAL]
    )
