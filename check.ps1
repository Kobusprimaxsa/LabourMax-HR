# Labourmax-HR - what is installed?
# Deliberately dumb and hard to break. Run:  .\check.ps1

Write-Host ""
Write-Host "Checking..." -ForegroundColor Cyan
Write-Host ""

$found = @{}

foreach ($name in @("py", "python", "python3", "psql", "git")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue

    if (-not $cmd) {
        Write-Host ("  {0,-8} not found" -f $name) -ForegroundColor DarkGray
        continue
    }

    $source = $cmd.Source

    if ($source -and $source -like "*WindowsApps*") {
        Write-Host ("  {0,-8} MICROSOFT STORE STUB - not real Python" -f $name) -ForegroundColor Red
        continue
    }

    $version = "?"
    try {
        $raw = & $source --version 2>&1
        $version = ($raw | Out-String).Trim()
    } catch {
        $version = "found but would not run"
    }

    Write-Host ("  {0,-8} {1}" -f $name, $version) -ForegroundColor Green
    Write-Host ("           {0}" -f $source) -ForegroundColor DarkGray
    $found[$name] = $true
}

Write-Host ""

$havePython = $found.ContainsKey("py") -or $found.ContainsKey("python") -or $found.ContainsKey("python3")
$havePsql   = $found.ContainsKey("psql")

if ($havePython) {
    Write-Host "Python: OK" -ForegroundColor Green
} else {
    Write-Host "Python: NOT INSTALLED" -ForegroundColor Red
    Write-Host "  https://www.python.org/downloads/windows/  - get 3.13, 64-bit installer" -ForegroundColor Gray
    Write-Host "  TICK 'Add python.exe to PATH' on the first installer screen" -ForegroundColor Yellow
}

if ($havePsql) {
    Write-Host "PostgreSQL: OK" -ForegroundColor Green
} else {
    Write-Host "PostgreSQL: NOT INSTALLED (or not on PATH)" -ForegroundColor Red
    Write-Host "  https://www.postgresql.org/download/windows/  - get version 17" -ForegroundColor Gray
}

Write-Host ""
if ($havePython -and $havePsql) {
    Write-Host "Both present. Next:  .\setup-windows.ps1" -ForegroundColor Cyan
} else {
    Write-Host "Install what is missing, then open a NEW PowerShell window and run this again." -ForegroundColor Yellow
    Write-Host "A window opened before the install will not see the new PATH." -ForegroundColor Yellow
}
Write-Host ""
