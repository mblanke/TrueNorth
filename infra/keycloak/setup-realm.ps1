<#
.SYNOPSIS
    Import the TrueNorth Keycloak realm into a running Keycloak instance.
.DESCRIPTION
    PowerShell equivalent of setup-realm.sh. Waits for Keycloak health,
    authenticates via the admin REST API, and imports realm-truenorth.json.
.PARAMETER KeycloakUrl
    Base URL of the Keycloak server. Default: http://localhost:8080
.PARAMETER AdminUser
    Keycloak master-realm admin username. Default: admin
.PARAMETER AdminPass
    Keycloak master-realm admin password. Default: admin
.EXAMPLE
    .\setup-realm.ps1
    .\setup-realm.ps1 -KeycloakUrl https://keycloak.truenorth.io -AdminUser kc_admin -AdminPass s3cret
#>
[CmdletBinding()]
param(
    [string]$KeycloakUrl = "http://localhost:8080",
    [string]$AdminUser   = "admin",
    [string]$AdminPass   = "admin"
)

$ErrorActionPreference = "Stop"
$RealmFile = Join-Path $PSScriptRoot "realm-truenorth.json"

Write-Host "========================================"  -ForegroundColor Cyan
Write-Host " TrueNorth Range - Keycloak Realm Setup" -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "Keycloak URL : $KeycloakUrl"
Write-Host "Admin user   : $AdminUser"
Write-Host "Realm file   : $RealmFile"
Write-Host ""

# ---- Helper: REST call ----
function Invoke-KC {
    param([string]$Method, [string]$Path, [object]$Body, [hashtable]$Headers = @{})
    $uri = "$KeycloakUrl$Path"
    $splat = @{
        Method      = $Method
        Uri         = $uri
        ContentType = "application/json"
        Headers     = $Headers
        ErrorAction = "Stop"
    }
    if ($Body) { $splat["Body"] = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 50 } }
    Invoke-RestMethod @splat
}

# ---- 1. Wait for Keycloak ----
Write-Host "[1/4] Waiting for Keycloak to become healthy..." -ForegroundColor Yellow
$maxRetries = 60
$interval   = 5
for ($i = 1; $i -le $maxRetries; $i++) {
    try {
        $null = Invoke-RestMethod -Uri "$KeycloakUrl/realms/master" -Method Get -ErrorAction Stop
        Write-Host "  Keycloak is healthy (attempt $i)." -ForegroundColor Green
        break
    } catch {
        if ($i -eq $maxRetries) {
            Write-Error "Keycloak did not become healthy after $($maxRetries * $interval)s."
            exit 1
        }
        Write-Host "  Attempt $i/$maxRetries - retrying in ${interval}s..."
        Start-Sleep -Seconds $interval
    }
}

# ---- 2. Authenticate ----
Write-Host "[2/4] Authenticating as '$AdminUser'..." -ForegroundColor Yellow
$tokenBody = @{
    grant_type = "password"
    client_id  = "admin-cli"
    username   = $AdminUser
    password   = $AdminPass
}
$tokenResp = Invoke-RestMethod -Uri "$KeycloakUrl/realms/master/protocol/openid-connect/token" `
    -Method Post -ContentType "application/x-www-form-urlencoded" `
    -Body $tokenBody -ErrorAction Stop
$token = $tokenResp.access_token
$authHeader = @{ Authorization = "Bearer $token" }
Write-Host "  Authenticated." -ForegroundColor Green

# ---- 3. Import realm ----
Write-Host "[3/4] Importing realm from $RealmFile..." -ForegroundColor Yellow
$realmJson = Get-Content -Path $RealmFile -Raw -Encoding UTF8

try {
    $existing = Invoke-RestMethod -Uri "$KeycloakUrl/admin/realms/truenorth" `
        -Method Get -Headers $authHeader -ErrorAction Stop
    Write-Host "  Realm 'truenorth' already exists - updating..."
    Invoke-RestMethod -Uri "$KeycloakUrl/admin/realms/truenorth" `
        -Method Put -Headers $authHeader -ContentType "application/json" `
        -Body $realmJson -ErrorAction Stop
} catch {
    if ($_.Exception.Response.StatusCode -eq 404 -or $_.Exception.Message -match "404") {
        Write-Host "  Creating realm 'truenorth'..."
        Invoke-RestMethod -Uri "$KeycloakUrl/admin/realms" `
            -Method Post -Headers $authHeader -ContentType "application/json" `
            -Body $realmJson -ErrorAction Stop
    } else {
        throw
    }
}
Write-Host "  Realm imported." -ForegroundColor Green

# ---- 4. Verify users ----
Write-Host "[4/4] Verifying platform users..." -ForegroundColor Yellow
$users = Invoke-RestMethod -Uri "$KeycloakUrl/admin/realms/truenorth/users?max=50" `
    -Method Get -Headers $authHeader -ErrorAction Stop
$devUsers = $users | Where-Object { $_.email -match "truenorth\.local$" }
Write-Host "  Found $($devUsers.Count) dev user(s) in realm." -ForegroundColor Green

Write-Host ""
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host " Setup Complete"                           -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host ""
Write-Host "  Keycloak Console : $KeycloakUrl/admin/master/console/"
Write-Host "  TrueNorth Realm  : $KeycloakUrl/admin/master/console/#/truenorth"
Write-Host ""
Write-Host "  Dev users:" -ForegroundColor White
Write-Host "    admin@truenorth.local      / admin      (admin role)"
Write-Host "    instructor@truenorth.local / instructor (instructor role)"
Write-Host "    trainee@truenorth.local    / trainee    (trainee role)"
Write-Host ""
Write-Host "  SPA client  : truenorth-web  (public, PKCE)"
Write-Host "  API client  : truenorth-api  (confidential, secret=CHANGE_ME_IN_PRODUCTION)"
Write-Host "  CLI client  : truenorth-cli  (public, device flow)"
Write-Host ""
Write-Host "  Token endpoint:"
Write-Host "    $KeycloakUrl/realms/truenorth/protocol/openid-connect/token"
Write-Host ""