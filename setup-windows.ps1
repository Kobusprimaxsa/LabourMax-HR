<#
    Labourmax-HR - local development bootstrap for Windows.

    Run .\check.ps1 first. This script assumes Python and PostgreSQL are installed.

        .\setup-windows.ps1
#>

Write-Host ""
Write-Host "Labourmax-HR local setup" -ForegroundColor Cyan
Write-Host "========================" -ForegroundColor Cyan
Write-Host ""

# --- find a real Python -------------------------------------------------------
# Windows ships a stub at ...\WindowsApps\python.exe that only opens the Store,
# so a name being on PATH proves nothing. Resolve to a real path and run it.

$pythonPath = $null
foreach ($name in @("py", "python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -and $cmd.Source -like "*WindowsApps*") { continue }
    $pythonPath = $cmd.Source
    break
}

if (-not $pythonPath) {
    Write-Host "Python is not installed." -ForegroundColor Red
    Write-Host ""
    Write-Host "  1. https://www.python.org/downloads/windows/  - Python 3.13, 64-bit installer" -ForegroundColor Gray
    Write-Host "  2. TICK 'Add python.exe to PATH' on the first screen" -ForegroundColor Yellow
    Write-Host "  3. Settings > Apps > Advanced app settings > App execution aliases" -ForegroundColor Gray
    Write-Host "     switch OFF python.exe and python3.exe" -ForegroundColor Gray
    Write-Host "  4. Open a NEW PowerShell window and run this again" -ForegroundColor Gray
    Write-Host ""
    exit 1
}

$pythonVersion = (& $pythonPath --version 2>&1 | Out-String).Trim()
Write-Host ("Using " + $pythonVersion) -ForegroundColor Green
Write-Host ("  " + $pythonPath) -ForegroundColor DarkGray
Write-Host ""

# --- virtualenv ---------------------------------------------------------------

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtualenv (.venv)..." -ForegroundColor Cyan
    & $pythonPath -m venv .venv
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "  Failed to create the virtualenv. Error above." -ForegroundColor Red
        exit 1
    }
    Write-Host "  done" -ForegroundColor Green
} else {
    Write-Host "Virtualenv already present." -ForegroundColor DarkGray
}

$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
Write-Host ""

# --- dependencies -------------------------------------------------------------

Write-Host "Installing dependencies (a minute or two, be patient)..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "pip failed. The error is above." -ForegroundColor Red
    exit 1
}
Write-Host "  done" -ForegroundColor Green
Write-Host ""

# --- .env ---------------------------------------------------------------------

if (-not (Test-Path ".env")) {
    Write-Host "Creating .env with generated keys..." -ForegroundColor Cyan
    Copy-Item .env.example .env

    $secret = & $venvPython -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    $fernet = & $venvPython -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    $content = Get-Content .env
    $content = $content -replace '^DJANGO_SECRET_KEY=.*', ("DJANGO_SECRET_KEY=" + $secret)
    $content = $content -replace '^FIELD_ENCRYPTION_KEY=.*', ("FIELD_ENCRYPTION_KEY=" + $fernet)
    Set-Content -Path .env -Value $content

    Write-Host "  .env created, keys generated" -ForegroundColor Green
} else {
    Write-Host ".env already present, leaving it alone." -ForegroundColor DarkGray
}
Write-Host ""

# --- next ---------------------------------------------------------------------

Write-Host "Next steps" -ForegroundColor Cyan
Write-Host "----------" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Create the database. Run:  psql -U postgres" -ForegroundColor White
Write-Host "   then paste these lines one at a time:" -ForegroundColor White
Write-Host ""
Write-Host "     CREATE ROLE labourmax WITH LOGIN PASSWORD 'pick-a-password';" -ForegroundColor Gray
Write-Host "     CREATE DATABASE labourmax OWNER labourmax;" -ForegroundColor Gray
Write-Host "     \c labourmax" -ForegroundColor Gray
Write-Host "     CREATE EXTENSION IF NOT EXISTS citext;" -ForegroundColor Gray
Write-Host "     CREATE EXTENSION IF NOT EXISTS btree_gist;" -ForegroundColor Gray
Write-Host "     CREATE EXTENSION IF NOT EXISTS pgcrypto;" -ForegroundColor Gray
Write-Host "     \q" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Open .env in Notepad, set POSTGRES_PASSWORD to that password." -ForegroundColor White
Write-Host ""
Write-Host "3. Activate the virtualenv:   .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host ""
Write-Host "4. Start Claude Code:         claude" -ForegroundColor White
Write-Host "   At the trust question, press DOWN ARROW then Enter." -ForegroundColor Gray
Write-Host ""
Write-Host "Never point the app at the postgres superuser role - a superuser" -ForegroundColor Yellow
Write-Host "bypasses row-level security and the isolation tests would pass" -ForegroundColor Yellow
Write-Host "while the protection did nothing." -ForegroundColor Yellow
Write-Host ""
