"""Business limits enforced in the database, where they cannot be bypassed.

A limit that lives only in a form is not a limit. It is a suggestion that holds
until someone writes a second code path — an invitation acceptance, an ownership
transfer, a support action, a data import — and every one of those is a separate
chance to forget.
"""

from __future__ import annotations

# Roles that occupy one of the account's paid administrative seats.
#
# ``employee`` is excluded: an employee self-service login sees only their own
# contract, payslips and IRP5 (decision D-21), so it is not an administrative
# user and an employer with forty cleaners is not buying forty seats.
#
# ``read_only`` IS included. It sees the whole company's payroll, so leaving it
# uncounted would make the limit trivially avoidable — invite as many read-only
# users as you like and read everything. If the commercial intent is that
# read-only logins are free, this tuple is the one place to change.
ADMINISTRATIVE_ROLES = ("owner", "admin", "read_only")

_ROLE_SQL_LIST = ", ".join(f"'{role}'" for role in ADMINISTRATIVE_ROLES)

ADMIN_SEAT_LIMIT_FUNCTION = f"""
CREATE OR REPLACE FUNCTION labourmax_enforce_admin_seat_limit() RETURNS trigger AS $$
DECLARE
    seat_limit integer;
    seats_used integer;
BEGIN
    IF NEW.revoked_at IS NOT NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.role NOT IN ({_ROLE_SQL_LIST}) THEN
        RETURN NEW;
    END IF;

    -- Lock the tenant row first. Two invitations accepted at the same instant
    -- would otherwise each count one seat in use, each find one free, and both
    -- insert. A counting check without a lock holds in testing and fails in
    -- production, which is the worst way for a limit to fail.
    SELECT max_admin_users INTO seat_limit
    FROM tenant
    WHERE id = NEW.tenant_id
    FOR UPDATE;

    IF seat_limit IS NULL THEN
        RAISE EXCEPTION 'tenant % does not exist', NEW.tenant_id;
    END IF;

    SELECT count(*) INTO seats_used
    FROM tenant_membership
    WHERE tenant_id = NEW.tenant_id
      AND revoked_at IS NULL
      AND role IN ({_ROLE_SQL_LIST})
      AND id IS DISTINCT FROM NEW.id;

    IF seats_used + 1 > seat_limit THEN
        RAISE EXCEPTION
            'administrative user limit reached: % of % seats in use for tenant %',
            seats_used, seat_limit, NEW.tenant_id
            USING HINT = 'Revoke an existing administrative user, or raise tenant.max_admin_users.',
                  ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""  # noqa: S608 - DDL from module constants, no user input, nothing to bind

DROP_ADMIN_SEAT_LIMIT_FUNCTION = "DROP FUNCTION IF EXISTS labourmax_enforce_admin_seat_limit();"

# BEFORE INSERT OR UPDATE with no column list on purpose. Naming columns is
# faster but silently stops enforcing the moment someone adds a field that can
# reactivate a membership.
ADMIN_SEAT_LIMIT_TRIGGER = """
DROP TRIGGER IF EXISTS tenant_membership_admin_seat_limit ON tenant_membership;
CREATE TRIGGER tenant_membership_admin_seat_limit
    BEFORE INSERT OR UPDATE ON tenant_membership
    FOR EACH ROW EXECUTE FUNCTION labourmax_enforce_admin_seat_limit();
"""

DROP_ADMIN_SEAT_LIMIT_TRIGGER = (
    "DROP TRIGGER IF EXISTS tenant_membership_admin_seat_limit ON tenant_membership;"
)
