<#
.SYNOPSIS
    Generates a .env file with fresh random secrets for the Sentinel stack.

.DESCRIPTION
    Creates .env from .env.example, substituting cryptographically random values
    for the Postgres passwords, the JWT signing secret and the credential master
    key. Refuses to overwrite an existing .env unless -Force is given, because
    regenerating CREDENTIAL_MASTER_KEY makes every stored camera credential
    undecryptable.

    Works on Windows PowerShell 5.1 (.NET Framework) and PowerShell 7+ (.NET).

.PARAMETER Local
    Also write DATABASE_URL and DATABASE_ADMIN_URL pointing at a PostgreSQL
    installed natively on this machine (localhost:5432). Use this when running
    without Docker. Under Docker Compose these two are injected by
    docker-compose.yml with host "db" instead, so they must NOT be in .env.

.PARAMETER Force
    Overwrite an existing .env. Destroys the ability to decrypt any camera
    credential already stored.

.EXAMPLE
    .\scripts\new-env.ps1
    .\scripts\new-env.ps1 -Local
#>
[CmdletBinding()]
param(
    [switch]$Local,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root '.env'
$examplePath = Join-Path $root '.env.example'

if ((Test-Path $envPath) -and -not $Force) {
    Write-Host ".env already exists. Not touching it." -ForegroundColor Yellow
    Write-Host "Re-run with -Force only if you accept that rotating" -ForegroundColor Yellow
    Write-Host "CREDENTIAL_MASTER_KEY orphans every stored camera credential." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $examplePath)) {
    throw ".env.example not found at $examplePath"
}

# RandomNumberGenerator.Create() exists on .NET Framework 4.x and on modern .NET.
# RandomNumberGenerator.Fill() is .NET Core only, so it cannot be used here:
# Windows PowerShell 5.1 ships with .NET Framework and would fail with
# "does not contain a method named 'Fill'".
function New-RandomBytes {
    param([int]$Count)
    $buf = New-Object byte[] $Count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buf)
    } finally {
        if ($rng -is [System.IDisposable]) { $rng.Dispose() }
    }
    return $buf
}

function New-UrlSafeSecret {
    param([int]$Bytes = 48)
    $buf = New-RandomBytes -Count $Bytes
    # URL-safe base64, padding stripped. This matters: these values are
    # interpolated into a postgresql:// connection string, and a raw '+' or '/'
    # would be misparsed.
    [Convert]::ToBase64String($buf).Replace('+', '-').Replace('/', '_').TrimEnd('=')
}

function New-Base64Key {
    param([int]$Bytes = 32)
    [Convert]::ToBase64String((New-RandomBytes -Count $Bytes))
}

$pgPassword  = New-UrlSafeSecret -Bytes 24
$appPassword = New-UrlSafeSecret -Bytes 24
$jwtSecret   = New-UrlSafeSecret -Bytes 48
$masterKey   = New-Base64Key -Bytes 32

$content = Get-Content $examplePath -Raw

$content = $content -replace 'POSTGRES_PASSWORD=.*',      "POSTGRES_PASSWORD=$pgPassword"
$content = $content -replace 'APP_DB_PASSWORD=.*',        "APP_DB_PASSWORD=$appPassword"
$content = $content -replace 'JWT_SECRET=.*',             "JWT_SECRET=$jwtSecret"
$content = $content -replace 'CREDENTIAL_MASTER_KEY=.*',  "CREDENTIAL_MASTER_KEY=$masterKey"

if ($Local) {
    $content += @"

# --- Native (no-Docker) connection strings ----------------------------------
# Added by scripts/new-env.ps1 -Local. Points at a PostgreSQL installed on this
# machine. Under Docker Compose these are supplied by docker-compose.yml with
# host "db" and must not appear here.
DATABASE_URL=postgresql://sentinel_app:$appPassword@localhost:5432/sentinel
DATABASE_ADMIN_URL=postgresql://postgres:$pgPassword@localhost:5432/sentinel
"@
}

# Write UTF-8 without BOM. Docker Compose does not strip a BOM and will treat it
# as part of the first variable name, which produces a baffling error.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)

Write-Host "Wrote $envPath with fresh secrets." -ForegroundColor Green
if ($Local) {
    Write-Host ""
    Write-Host "Native mode. Next: .\scripts\setup-local.ps1" -ForegroundColor Cyan
    Write-Host "The Postgres superuser password you must type into the installer is:" -ForegroundColor Cyan
    Write-Host "  $pgPassword" -ForegroundColor White
}
Write-Host ""
Write-Host "Back up CREDENTIAL_MASTER_KEY somewhere safe. Losing it means every" -ForegroundColor Cyan
Write-Host "encrypted camera credential in the database becomes unrecoverable." -ForegroundColor Cyan
