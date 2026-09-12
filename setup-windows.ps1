<#
    Labourmax-HR — local development bootstrap for Windows.

    Assumes Python 3.13 and PostgreSQL 17 are already installed and on PATH.
    See README.md for those steps.

    Run from the project root:
        .\setup-windows.ps1
#>

$ErrorActionPreference = "Stop"

Write-Host "Labourmax-HR local setup" -ForegroundColor Cyan
Write-Host ""

# --- checks -------------------------------------------------------------------
function Require-Command($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Host "MISSING: $name" -ForegroundColor Red
        Write-Host "  $hint"
        exit 1
    }
    Write-Host "  found $name" -ForegroundColor DarkGray
}

Require-Command "python" "Install Python 3.13 and tick 'Add Python to PATH'."
Require-Command "psql"   "Install PostgreSQL 17 and let it add bin\ to PATH."

$pyVersion = (python --version)
Write-Host "  $pyVersion" -ForegroundColor DarkGray
Write-Host ""

# --- virtualenv ---------------------------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtualenv..." -ForegroundColor Cyan
    python -m venv .venv
} else {
    Write-Host "Virtualenv already present." -ForegroundColor DarkGray
}

& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip --quiet
Write-Host "Installing dependencies..." -ForegroundColor Cyan
pip install -r requirements-dev.txt --quiet
Write-Host "  done" -ForegroundColor DarkGray
Write-Host ""

# --- .env ---------------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env with generated keys..." -ForegroundColor Cyan
    Copy-Item .env.example .env

    $secret = python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    $fernet = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    (Get-Content .env) `
        -replace '^DJANGO_SECRET_KEY=.*', "DJANGO_SECRET_KEY=$secret" `
        -replace '^FIELD_ENCRYPTION_KEY=.*', "FIELD_ENCRYPTION_KEY=$fernet" |
        Set-Content .env

    Write-Host "  .env created. Set POSTGRES_PASSWORD before migrating." -ForegroundColor Yellow
} else {
    Write-Host ".env already present, leaving it alone." -ForegroundColor DarkGray
}
Write-Host ""

# --- database -----------------------------------------------------------------
Write-Host "Database setup is manual, on purpose. In psql as postgres:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  CREATE ROLE labourmax WITH LOGIN PASSWORD 'your-password';" -ForegroundColor White
Write-Host "  CREATE DATABASE labourmax OWNER labourmax;" -ForegroundColor White
Write-Host "  \c labourmax" -ForegroundColor White
Write-Host "  CREATE EXTENSION IF NOT EXISTS citext;" -ForegroundColor White
Write-Host "  CREATE EXTENSION IF NOT EXISTS btree_gist;" -ForegroundColor White
Write-Host "  CREATE EXTENSION IF NOT EXISTS pgcrypto;" -ForegroundColor White
Write-Host ""
Write-Host "Then:" -ForegroundColor Cyan
Write-Host "  python manage.py migrate" -ForegroundColor White
Write-Host "  python manage.py createsuperuser" -ForegroundColor White
Write-Host "  python manage.py runserver" -ForegroundColor White
Write-Host ""
Write-Host "Never point the application at the postgres superuser role —" -ForegroundColor Yellow
Write-Host "a superuser bypasses row-level security and the isolation tests" -ForegroundColor Yellow
Write-Host "would pass while the protection does nothing." -ForegroundColor Yellow
