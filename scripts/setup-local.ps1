<#
.SYNOPSIS
    Brings up the Sentinel stack natively on Windows, without Docker.

.DESCRIPTION
    Docker Desktop requires WSL 2 or Hyper-V. On a machine where `wsl --install`
    is blocked by policy, neither is available, so this script provides the same
    outcome using a PostgreSQL installed directly on Windows plus a local Python
    virtual environment.

    It performs the work that docker-compose.yml and db/bootstrap/00-app-role.sh
    would otherwise do:
      1. locate psql from a Windows PostgreSQL installation
      2. create the sentinel database
      3. enable the postgis extension
      4. create the least-privilege sentinel_app role (NOSUPERUSER, NOBYPASSRLS)
      5. revoke the default PUBLIC grants
      6. create .venv and install requirements
      7. run the migrations

    Idempotent: safe to re-run. Every step checks before it acts.

.PARAMETER PgBin
    Path to the PostgreSQL bin directory, if auto-detection fails.
    e.g. -PgBin "C:\Program Files\PostgreSQL\16\bin"

.PARAMETER SkipMigrate
    Set up the database and venv but do not run migrations.

.EXAMPLE
    .\scripts\setup-local.ps1
#>
[CmdletBinding()]
param(
    [string]$PgBin,
    [switch]$SkipMigrate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root '.env'

function Write-Step { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "    $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "    $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 0. Read .env
# ---------------------------------------------------------------------------
Write-Step "Reading .env"

if (-not (Test-Path $envPath)) {
    throw ".env not found. Run this first:`n    .\scripts\new-env.ps1 -Local"
}

# Parse .env into a hashtable. Deliberately simple: KEY=VALUE, ignoring blanks
# and comments. The file is machine-generated, so it does not need a real parser.
$envVars = @{}
foreach ($line in Get-Content $envPath) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $idx = $trimmed.IndexOf('=')
    if ($idx -lt 1) { continue }
    $envVars[$trimmed.Substring(0, $idx).Trim()] = $trimmed.Substring($idx + 1).Trim()
}

$dbName      = if ($envVars['POSTGRES_DB'])   { $envVars['POSTGRES_DB'] }   else { 'sentinel' }
$pgUser      = if ($envVars['POSTGRES_USER']) { $envVars['POSTGRES_USER'] } else { 'postgres' }
$pgPassword  = $envVars['POSTGRES_PASSWORD']
$appUser     = if ($envVars['APP_DB_USER'])   { $envVars['APP_DB_USER'] }   else { 'sentinel_app' }
$appPassword = $envVars['APP_DB_PASSWORD']

foreach ($pair in @(
    @{ n = 'POSTGRES_PASSWORD'; v = $pgPassword },
    @{ n = 'APP_DB_PASSWORD';   v = $appPassword }
)) {
    if (-not $pair.v -or $pair.v -like '*CHANGE_ME*') {
        throw "$($pair.n) is missing or still the placeholder in .env. Run: .\scripts\new-env.ps1 -Local -Force"
    }
}

if (-not $envVars.ContainsKey('DATABASE_URL')) {
    Write-Warn "DATABASE_URL is not in .env. Appending native connection strings."
    $add = @"

# --- Native (no-Docker) connection strings ----------------------------------
DATABASE_URL=postgresql://${appUser}:${appPassword}@localhost:5432/${dbName}
DATABASE_ADMIN_URL=postgresql://${pgUser}:${pgPassword}@localhost:5432/${dbName}
"@
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($envPath, $add, $utf8NoBom)
    Write-Ok "Appended DATABASE_URL and DATABASE_ADMIN_URL."
}

Write-Ok "database=$dbName  superuser=$pgUser  app role=$appUser"

# ---------------------------------------------------------------------------
# 1. Locate psql
# ---------------------------------------------------------------------------
Write-Step "Locating PostgreSQL"

if (-not $PgBin) {
    $onPath = Get-Command psql -ErrorAction SilentlyContinue
    if ($onPath) {
        $PgBin = Split-Path -Parent $onPath.Source
    } else {
        # Highest version number first, so a machine with 15 and 16 installed
        # picks 16.
        $candidates = Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue |
            Sort-Object { [int]($_.Name -replace '\D', '0') } -Descending
        foreach ($c in $candidates) {
            $try = Join-Path $c.FullName 'bin'
            if (Test-Path (Join-Path $try 'psql.exe')) { $PgBin = $try; break }
        }
    }
}

if (-not $PgBin -or -not (Test-Path (Join-Path $PgBin 'psql.exe'))) {
    Write-Host ""
    Write-Host "PostgreSQL was not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it with PostGIS, which the schema requires:" -ForegroundColor Yellow
    Write-Host "  1. Download the EDB installer for PostgreSQL 16:"
    Write-Host "     https://www.enterprisedb.com/downloads/postgres-postgresql-downloads"
    Write-Host "  2. During install, set the postgres superuser password to the"
    Write-Host "     POSTGRES_PASSWORD value in your .env file."
    Write-Host "  3. Keep port 5432 (the default)."
    Write-Host "  4. At the end, let Stack Builder run and tick:"
    Write-Host "         Spatial Extensions -> PostGIS ... Bundle"
    Write-Host "     PostGIS is not optional here. The registry stores camera"
    Write-Host "     geometry and computes coverage in the database."
    Write-Host "  5. Re-run this script."
    Write-Host ""
    Write-Host "If it is installed somewhere unusual, pass the path:" -ForegroundColor Yellow
    Write-Host '     .\scripts\setup-local.ps1 -PgBin "D:\PostgreSQL\16\bin"'
    throw "psql.exe not found"
}

$psql = Join-Path $PgBin 'psql.exe'
Write-Ok "psql at $psql"

# PGPASSWORD avoids an interactive prompt. Scoped to this process only, so it
# does not persist into the user's environment.
$env:PGPASSWORD = $pgPassword

function Invoke-Psql {
    param(
        [Parameter(Mandatory)][string]$Database,
        [Parameter(Mandatory)][string]$Sql,
        [switch]$TupleOnly
    )
    $args = @('--username', $pgUser, '--host', 'localhost', '--port', '5432',
              '--dbname', $Database, '-v', 'ON_ERROR_STOP=1', '--no-psqlrc')
    if ($TupleOnly) { $args += @('-t', '-A') }
    $args += @('-c', $Sql)
    $out = & $psql @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "psql failed:`n$($out -join "`n")"
    }
    return ($out -join "`n").Trim()
}

# ---------------------------------------------------------------------------
# 2. Confirm the server answers
# ---------------------------------------------------------------------------
Write-Step "Connecting to the server"
try {
    $version = Invoke-Psql -Database 'postgres' -Sql 'SELECT version()' -TupleOnly
} catch {
    Write-Host ""
    Write-Host "Could not connect as $pgUser on localhost:5432." -ForegroundColor Red
    Write-Host "Two usual causes:" -ForegroundColor Yellow
    Write-Host "  - The service is stopped. Start it:"
    Write-Host "        Get-Service postgresql* | Start-Service"
    Write-Host "  - POSTGRES_PASSWORD in .env does not match the password you set"
    Write-Host "    during installation. Either correct .env, or reset the password:"
    Write-Host "        ALTER USER postgres WITH PASSWORD '<the value in .env>';"
    throw
}
Write-Ok ($version -split ',')[0]

# ---------------------------------------------------------------------------
# 3. Create the database
# ---------------------------------------------------------------------------
Write-Step "Creating database '$dbName'"
$exists = Invoke-Psql -Database 'postgres' -TupleOnly `
    -Sql "SELECT 1 FROM pg_database WHERE datname = '$dbName'"
if ($exists -eq '1') {
    Write-Ok "Already exists."
} else {
    # CREATE DATABASE cannot run inside a transaction block, hence its own call.
    Invoke-Psql -Database 'postgres' -Sql "CREATE DATABASE `"$dbName`"" | Out-Null
    Write-Ok "Created."
}

# ---------------------------------------------------------------------------
# 4. Enable PostGIS
# ---------------------------------------------------------------------------
Write-Step "Enabling PostGIS"
$hasPostgis = Invoke-Psql -Database $dbName -TupleOnly `
    -Sql "SELECT 1 FROM pg_available_extensions WHERE name = 'postgis'"
if ($hasPostgis -ne '1') {
    Write-Host ""
    Write-Host "PostGIS is not available on this server." -ForegroundColor Red
    Write-Host "Run Stack Builder from the Start menu ('Application Stack Builder')," -ForegroundColor Yellow
    Write-Host "choose your PostgreSQL 16 installation, and install:" -ForegroundColor Yellow
    Write-Host "    Spatial Extensions -> PostGIS ... Bundle"
    Write-Host "Then re-run this script."
    throw "postgis extension unavailable"
}
Invoke-Psql -Database $dbName -Sql 'CREATE EXTENSION IF NOT EXISTS postgis' | Out-Null
$pgv = Invoke-Psql -Database $dbName -Sql 'SELECT postgis_version()' -TupleOnly
Write-Ok "PostGIS $pgv"

# ---------------------------------------------------------------------------
# 5. Create the least-privilege app role
#    Mirrors db/bootstrap/00-app-role.sh, which only runs inside the container.
# ---------------------------------------------------------------------------
Write-Step "Creating role '$appUser'"

# Doubled single quotes: this SQL is embedded in a PowerShell string that is
# itself passed to psql -c, and the password must survive both layers intact.
$appPwSql = $appPassword.Replace("'", "''")

$roleSql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$appUser') THEN
        CREATE ROLE $appUser LOGIN PASSWORD '$appPwSql'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ELSE
        ALTER ROLE $appUser WITH LOGIN PASSWORD '$appPwSql'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
`$`$;
REVOKE ALL ON DATABASE "$dbName" FROM PUBLIC;
GRANT CONNECT ON DATABASE "$dbName" TO $appUser;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
"@

Invoke-Psql -Database $dbName -Sql $roleSql | Out-Null
Write-Ok "Role ready, NOSUPERUSER and NOBYPASSRLS confirmed."

# A superuser or a BYPASSRLS role would silently defeat every row-level policy in
# 004_rls.sql, so verify rather than assume.
$badAttrs = Invoke-Psql -Database $dbName -TupleOnly -Sql @"
SELECT rolsuper::text || ',' || rolbypassrls::text
FROM pg_roles WHERE rolname = '$appUser'
"@
if ($badAttrs -ne 'false,false') {
    throw "Role $appUser has rolsuper/rolbypassrls = $badAttrs. RLS would not apply. Refusing to continue."
}
Write-Ok "Verified: RLS will apply to this role."

$env:PGPASSWORD = $null

# ---------------------------------------------------------------------------
# 6. Python virtual environment
# ---------------------------------------------------------------------------
Write-Step "Setting up Python environment"

$py = Get-Command py -ErrorAction SilentlyContinue
$pyExe = if ($py) { 'py' } else { 'python' }
if (-not (Get-Command $pyExe -ErrorAction SilentlyContinue)) {
    throw "Python not found. Install Python 3.11+ from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
}

$venv = Join-Path $root '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $venvPy)) {
    Write-Ok "Creating .venv"
    & $pyExe -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Ok ".venv already exists"
}

Write-Ok "Installing requirements (this takes a minute)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root 'services\registry\requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed. If it was a network error, retry. If psycopg failed to build, confirm requirements.txt pins psycopg[binary], not bare psycopg."
}

# The exact failure the bare-psycopg install produces is an obscure runtime
# ImportError, so prove the driver actually works before moving on.
& $venvPy -c "import psycopg; print('psycopg', psycopg.__version__, 'ok')"
if ($LASTEXITCODE -ne 0) {
    throw "psycopg imported but has no libpq. Fix with: .\.venv\Scripts\python.exe -m pip install --force-reinstall `"psycopg[binary]`""
}
Write-Ok "Dependencies installed."

# ---------------------------------------------------------------------------
# 7. Migrations
# ---------------------------------------------------------------------------
if ($SkipMigrate) {
    Write-Step "Skipping migrations (-SkipMigrate)"
} else {
    Write-Step "Running migrations"
    Push-Location (Join-Path $root 'services\registry')
    try {
        & $venvPy -m app.migrate up
        if ($LASTEXITCODE -ne 0) { throw "migrations failed" }
    } finally {
        Pop-Location
    }
    Write-Ok "Schema applied."
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Local stack ready." -ForegroundColor Green
Write-Host ""
Write-Host "Start the API:" -ForegroundColor Cyan
Write-Host "    cd services\registry"
Write-Host "    ..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000"
Write-Host ""
Write-Host "Then check:" -ForegroundColor Cyan
Write-Host "    curl http://localhost:8000/api/ready"
Write-Host ""
Write-Host "Run the unit tests (no database needed):" -ForegroundColor Cyan
Write-Host "    cd services\registry"
Write-Host "    ..\..\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v"
