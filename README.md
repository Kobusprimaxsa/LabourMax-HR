# Labourmax-HR

Multi-tenant HR and payroll for the South African domestic worker and contract cleaning
sectors. Django 5.2 on PostgreSQL.

**Read `CLAUDE.md` before writing any code.** It carries the invariants, and each one exists
because of a specific way payroll systems go wrong.

- `docs/PHASES.md` — the work breakdown, twelve phases
- `docs/DECISIONS.md` — every settled decision and what is still open
- The Development Plan and Database Specification documents are the authoritative specs

---

## Local setup on Windows

### 1. PostgreSQL

Download the installer from <https://www.postgresql.org/download/windows/> — version 17.
During installation:

- Note the password you set for the `postgres` superuser
- Keep the default port `5432`
- Install **pgAdmin 4** alongside it (a GUI is worth having)
- Let the installer add PostgreSQL's `bin` directory to your PATH

Then create the database and its role. In PowerShell:

```powershell
psql -U postgres
```

```sql
CREATE ROLE labourmax WITH LOGIN PASSWORD 'choose-a-real-password';
CREATE DATABASE labourmax OWNER labourmax;
\c labourmax
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- exclusion constraints on effective-dated ranges
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()
\q
```

`btree_gist` is not optional. Without it the exclusion constraints that stop two overlapping
pay-rate rows for the same employee cannot be created.

**One thing to know about row-level security.** The application connects as the table owner,
and an owner bypasses RLS unless the policy is FORCED. The migrations do force it — but it
also means a superuser connection (`postgres`) ignores the policies entirely. Never point the
application at the `postgres` role, not even in development, or the isolation tests will pass
while the protection does nothing.

### 2. Python

Python 3.13 from <https://www.python.org/downloads/windows/> — tick "Add Python to PATH".

```powershell
cd C:\projects\LabourMax-HR
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

If PowerShell blocks the activation script:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### 3. Environment

```powershell
Copy-Item .env.example .env
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the first into `DJANGO_SECRET_KEY`, the second into `FIELD_ENCRYPTION_KEY`, and set
`POSTGRES_PASSWORD` to the role password you chose. **`.env` is gitignored — keep it that way.**

### 4. Run

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

### 5. Redis (optional until P1)

Celery is not needed until background jobs arrive in P1. When you get there, either run Redis
under WSL2 or use Memurai for Windows.

---

## Daily commands

```powershell
.\.venv\Scripts\Activate.ps1

python manage.py runserver
python manage.py makemigrations
python manage.py migrate

pytest                                     # everything
pytest -m isolation -v                     # the suite that must never fail
pytest --cov=calculators --cov-fail-under=100
ruff check . && ruff format .
```

---

## Before every commit

1. `ruff check .` clean
2. `pytest -m isolation` passing
3. `python manage.py makemigrations --check --dry-run` reports nothing outstanding
4. Commit message carries the phase: `P0: add TenantScopedManager`
