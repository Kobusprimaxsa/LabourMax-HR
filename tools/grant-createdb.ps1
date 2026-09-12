# grant-createdb.ps1
#
# One-time local development fix. Two things need a PostgreSQL superuser:
#
#   1. the labourmax role needs CREATEDB, because pytest builds and drops a
#      separate test_labourmax database on every run
#   2. the postgres password is unknown, so it gets reset to one you choose
#
# Neither can be done by the labourmax role itself. This script switches local
# authentication to 'trust' for the duration, does the work, and switches it
# back in the same run so the window where no password is required is seconds
# rather than indefinite.
#
# MUST be run in an Administrator PowerShell: pg_hba.conf lives under
# C:\Program Files. Pure ASCII on purpose - Windows PowerShell reads .ps1 as
# ANSI, so a stray em-dash breaks parsing with a confusing error.
#
# Usage:  .\tools\grant-createdb.ps1

$ErrorActionPreference = "Stop"

$pgVersion = "18"
$pgBin  = "C:\Program Files\PostgreSQL\$pgVersion\bin"
$hba    = "C:\Program Files\PostgreSQL\$pgVersion\data\pg_hba.conf"
$service = "postgresql-x64-$pgVersion"

function Fail($message) {
    Write-Host ""
    Write-Host "FAILED: $message" -ForegroundColor Red
    exit 1
}

# --- preflight -------------------------------------------------------------

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "not running as Administrator. Right-click Start, choose Terminal (Admin) or Windows PowerShell (Admin), then run this again."
}

if (-not (Test-Path $hba)) { Fail "pg_hba.conf not found at $hba" }
if (-not (Test-Path "$pgBin\psql.exe")) { Fail "psql.exe not found at $pgBin" }

$env:Path = "$pgBin;" + $env:Path

Write-Host "PostgreSQL $pgVersion found. Service: $service"
Write-Host ""

$newPostgresPassword = Read-Host "Choose a NEW password for the postgres superuser (no # character)"
if ([string]::IsNullOrWhiteSpace($newPostgresPassword)) { Fail "no password entered" }
if ($newPostgresPassword.Contains("#")) { Fail "'#' starts a comment in .env files. Choose a password without it." }

# --- do the work -----------------------------------------------------------

$backup = "$hba.backup-createdb"
$reverted = $false

try {
    Write-Host "Backing up pg_hba.conf to $backup"
    Copy-Item $hba $backup -Force

    Write-Host "Switching local authentication to trust"
    (Get-Content $hba) -replace 'scram-sha-256', 'trust' | Set-Content $hba -Encoding ascii

    Write-Host "Restarting $service"
    Restart-Service $service
    Start-Sleep -Seconds 4

    Write-Host "Checking passwordless access"
    # psql writes failures to stderr, and a native command writing to stderr
    # throws while $ErrorActionPreference is "Stop". Relax it just for the probe.
    $ErrorActionPreference = "Continue"
    $who = (& psql -U postgres -At -c "SELECT current_user" 2>&1) -join " "
    $ErrorActionPreference = "Stop"
    if ($who -notmatch "^postgres") { Fail "expected 'postgres', got: $who" }

    Write-Host "Granting CREATEDB to labourmax"
    & psql -U postgres -c "ALTER ROLE labourmax CREATEDB;" | Out-Host

    Write-Host "Setting the postgres password"
    & psql -U postgres -c "ALTER USER postgres PASSWORD '$newPostgresPassword';" | Out-Host
}
finally {
    if (Test-Path $backup) {
        Write-Host ""
        Write-Host "Restoring password authentication"
        Copy-Item $backup $hba -Force
        Restart-Service $service
        Start-Sleep -Seconds 4
        $reverted = $true
    }
}

if (-not $reverted) { Fail "pg_hba.conf was NOT restored. Restore $backup manually before doing anything else." }

# --- verify ----------------------------------------------------------------

Write-Host ""
Write-Host "Verifying that password authentication is back on"
$env:PGPASSWORD = "definitely-not-the-password"
$ErrorActionPreference = "Continue"
$probe = (& psql -U postgres -c "SELECT 1;" 2>&1) -join " "
$ErrorActionPreference = "Stop"
Remove-Item Env:\PGPASSWORD
if ($probe -notmatch "password authentication failed") {
    Fail "a deliberately wrong password was NOT rejected. Local authentication may still be set to trust. Inspect $hba."
}
Write-Host "  a wrong password is rejected - good"

Write-Host ""
Write-Host "Verifying the CREATEDB grant"
$env:PGPASSWORD = $newPostgresPassword
$ErrorActionPreference = "Continue"
$canCreate = (& psql -U postgres -At -c "SELECT rolcreatedb FROM pg_roles WHERE rolname = 'labourmax';" 2>&1) -join " "
$ErrorActionPreference = "Stop"
Remove-Item Env:\PGPASSWORD
if ($canCreate.Trim() -ne "t") { Fail "labourmax does not have CREATEDB (got: $canCreate)" }
Write-Host "  labourmax can create databases - good"

Write-Host ""
Write-Host "DONE. Write the postgres password down somewhere you will find it again." -ForegroundColor Green
Write-Host "You can now run: pytest -m isolation -v"
