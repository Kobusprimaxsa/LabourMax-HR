"""``python manage.py dbcheck`` — is the local environment actually wired up?

Reports what Django parsed out of ``.env``, whether the connection opens, the
server version, and whether the extensions the schema depends on are present.
Deliberately prints nothing secret: the password is shown only as a length,
which is enough to catch the ``#``-truncation trap in ``.env`` without putting
the value on screen or into a shell history.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

# D-137: btree_gist only. It backs every ExclusionConstraint in this schema and is
# installed by statutory/0002. citext and pgcrypto are not: hashing and encryption
# are Python-side (hashlib, hmac, Fernet) because D-77 keeps the key out of the
# database, and D-98 abandoned citext. The migration that needs an extension is the
# enforcement point — this list is not a wishlist, so it names only what a
# migration has actually installed. Anything needing citext or pgcrypto later adds
# the Extension() operation to the migration that needs it, and this tuple grows
# with it.
REQUIRED_EXTENSIONS = ("btree_gist",)


class Command(BaseCommand):
    help = "Verify the database connection, server version and required extensions."

    def handle(self, *args, **options):
        cfg = connection.settings_dict
        password = cfg.get("PASSWORD") or ""

        self.stdout.write("Configuration Django parsed:")
        self.stdout.write(f"  ENGINE   {cfg['ENGINE']}")
        self.stdout.write(f"  NAME     {cfg['NAME']!r}")
        self.stdout.write(f"  USER     {cfg['USER']!r}")
        self.stdout.write(f"  HOST     {cfg['HOST']!r}")
        self.stdout.write(f"  PORT     {cfg['PORT']!r}")
        self.stdout.write(f"  PASSWORD {len(password)} characters")
        if not password:
            self.stdout.write(
                self.style.WARNING(
                    "  -> empty. Check POSTGRES_PASSWORD in .env. Note that '#' starts a "
                    "comment there, so a password containing it is silently truncated."
                )
            )

        self.stdout.write("")
        try:
            connection.ensure_connection()
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Connection FAILED: {exc}"))
            raise SystemExit(1) from exc

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, version()")
            database, user, version = cursor.fetchone()
            cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cursor.fetchone()
            is_superuser = bool(row and row[0])
            cursor.execute("SELECT extname FROM pg_extension")
            installed = {r[0] for r in cursor.fetchall()}

        self.stdout.write(self.style.SUCCESS(f"Connected to {database!r} as {user!r}"))
        self.stdout.write(f"  {version.split(' on ')[0]}")

        self.stdout.write("")
        self.stdout.write("Extensions:")
        missing = []
        for name in REQUIRED_EXTENSIONS:
            if name in installed:
                self.stdout.write(self.style.SUCCESS(f"  present  {name}"))
            else:
                missing.append(name)
                self.stdout.write(self.style.ERROR(f"  MISSING  {name}"))

        self.stdout.write("")
        if is_superuser:
            # CLAUDE.md, non-negotiable 1: a superuser bypasses row-level security,
            # so the isolation suite would pass while protecting nothing.
            self.stdout.write(
                self.style.ERROR(
                    f"REFUSE: {user!r} is a PostgreSQL superuser. A superuser bypasses "
                    "row-level security, so tenant isolation would be silently inert "
                    "and the isolation tests would still pass. Point the application "
                    "at an ordinary role that owns the tables."
                )
            )
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(f"Role {user!r} is not a superuser - correct."))

        if missing:
            joined = " ".join(f"CREATE EXTENSION IF NOT EXISTS {n};" for n in missing)
            self.stdout.write("")
            self.stdout.write(
                self.style.ERROR(
                    "Create the missing extensions as the postgres superuser:\n"
                    f'  psql -U postgres -d {database} -c "{joined}"'
                )
            )
            raise SystemExit(1)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Environment OK."))
