<#
.SYNOPSIS
    Run TrueNorth Range k6 load test scenarios.

.DESCRIPTION
    Executes k6 scenarios in order: smoke -> load -> stress -> spike.
    Stops on first failure unless -Force is specified.
    Outputs HTML summary reports to the results/ directory.

.PARAMETER Scenario
    Run only a specific scenario. Valid values:
    smoke, load, stress, spike, soak, websocket, batch-provision

.PARAMETER Force
    Continue running subsequent scenarios even if one fails.

.PARAMETER BaseUrl
    Override the API base URL (default: http://localhost:8080).

.PARAMETER Token
    Override the auth token.

.EXAMPLE
    .\run-all.ps1
    .\run-all.ps1 -Scenario smoke
    .\run-all.ps1 -Scenario stress -BaseUrl http://staging:8080
#>

[CmdletBinding()]
param(
    [ValidateSet("smoke","load","stress","spike","soak","websocket","batch-provision")]
    [string]$Scenario,

    [switch]$Force,

    [string]$BaseUrl = "http://localhost:8080",

    [string]$Token = "dev-test-token"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$resultsDir = Join-Path $scriptDir "results"

# Ensure results directory exists
if (-not (Test-Path $resultsDir)) {
    New-Item -ItemType Directory -Path $resultsDir -Force | Out-Null
}

# Check k6 is installed
if (-not (Get-Command k6 -ErrorAction SilentlyContinue)) {
    Write-Error "k6 is not installed or not in PATH. Install from https://k6.io/docs/getting-started/installation/"
    exit 1
}

# Scenario definitions (order matters for default run)
$allScenarios = [ordered]@{
    "smoke"           = "scenarios/smoke.js"
    "load"            = "scenarios/load.js"
    "stress"          = "scenarios/stress.js"
    "spike"           = "scenarios/spike.js"
    "soak"            = "scenarios/soak.js"
    "websocket"       = "scenarios/websocket.js"
    "batch-provision" = "scenarios/batch-provision.js"
}

# Determine which scenarios to run
if ($Scenario) {
    $toRun = [ordered]@{ $Scenario = $allScenarios[$Scenario] }
} else {
    # Default pipeline: smoke -> load -> stress -> spike (skip soak/ws/batch by default)
    $toRun = [ordered]@{
        "smoke"  = $allScenarios["smoke"]
        "load"   = $allScenarios["load"]
        "stress" = $allScenarios["stress"]
        "spike"  = $allScenarios["spike"]
    }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$totalPassed = 0
$totalFailed = 0

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  TrueNorth Range — k6 Load Test Runner" -ForegroundColor Cyan
Write-Host "  Base URL : $BaseUrl" -ForegroundColor Cyan
Write-Host "  Timestamp: $timestamp" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

foreach ($name in $toRun.Keys) {
    $scriptPath = Join-Path $scriptDir $toRun[$name]
    $reportFile = Join-Path $resultsDir "${name}_${timestamp}.html"
    $jsonFile   = Join-Path $resultsDir "${name}_${timestamp}.json"

    Write-Host "────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host "  Running: $name" -ForegroundColor Yellow
    Write-Host "  Script : $scriptPath" -ForegroundColor DarkGray
    Write-Host "  Report : $reportFile" -ForegroundColor DarkGray
    Write-Host "────────────────────────────────────────────────────────────" -ForegroundColor DarkGray

    $env:BASE_URL   = $BaseUrl
    $env:WS_URL     = $BaseUrl -replace "^http", "ws"
    $env:AUTH_TOKEN  = $Token

    # Run k6 with HTML summary output and JSON summary
    & k6 run $scriptPath `
        --summary-export="$jsonFile" `
        --out "json=$($resultsDir)\${name}_${timestamp}_raw.json" `
        2>&1 | Tee-Object -Variable k6Output

    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        Write-Host "  PASSED: $name" -ForegroundColor Green
        $totalPassed++
    } else {
        Write-Host "  FAILED: $name (exit code $exitCode)" -ForegroundColor Red
        $totalFailed++

        if (-not $Force) {
            Write-Host ""
            Write-Host "  Stopping — use -Force to continue after failures." -ForegroundColor Red
            break
        }
    }

    Write-Host ""
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Results Summary" -ForegroundColor Cyan
Write-Host "  Passed : $totalPassed" -ForegroundColor Green
Write-Host "  Failed : $totalFailed" -ForegroundColor $(if ($totalFailed -gt 0) { "Red" } else { "Green" })
Write-Host "  Reports: $resultsDir" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

exit $totalFailed