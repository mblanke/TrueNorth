#!/usr/bin/env pwsh
# TrueNorth Range — Packer Build Orchestrator
# Validates and builds all Packer templates in dependency order.
#
# Usage:
#   .\build-all.ps1                       # Build all templates
#   .\build-all.ps1 -Template kali-linux  # Build a single template
#   .\build-all.ps1 -ValidateOnly        # Validate only, no build
#
param(
    [Parameter(Position = 0)]
    [ValidateSet(
        "ubuntu-2204",
        "windows-server-2022",
        "windows-10-workstation",
        "kali-linux",
        "security-onion",
        "pfsense"
    )]
    [string]$Template,

    [switch]$ValidateOnly,

    [string]$VarFile
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Build order: infrastructure images first, then dependent workloads
$BuildOrder = @(
    "pfsense"
    "ubuntu-2204"
    "security-onion"
    "kali-linux"
    "windows-server-2022"
    "windows-10-workstation"
)

# Map template names to HCL files
$TemplateFiles = @{
    "ubuntu-2204"           = "ubuntu-2204.pkr.hcl"
    "windows-server-2022"   = "windows-server-2022.pkr.hcl"
    "windows-10-workstation" = "windows-10-workstation.pkr.hcl"
    "kali-linux"            = "kali-linux.pkr.hcl"
    "security-onion"        = "security-onion.pkr.hcl"
    "pfsense"               = "pfsense.pkr.hcl"
}

# Create logs directory
$LogDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

# Resolve which templates to process
if ($Template) {
    $TemplatesToBuild = @($Template)
} else {
    $TemplatesToBuild = $BuildOrder
}

# Check packer is available
$PackerCmd = Get-Command packer -ErrorAction SilentlyContinue
if (-not $PackerCmd) {
    Write-Error "packer executable not found in PATH. Install Packer from https://www.packer.io/downloads"
    exit 1
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " TrueNorth Range — Packer Build Pipeline"    -ForegroundColor Cyan
Write-Host " Templates: $($TemplatesToBuild -join ', ')" -ForegroundColor Cyan
Write-Host " Timestamp: $Timestamp"                      -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$FailedTemplates = @()
$SuccessTemplates = @()

foreach ($tpl in $TemplatesToBuild) {
    $hclFile = $TemplateFiles[$tpl]
    $hclPath = Join-Path $ScriptDir $hclFile

    if (-not (Test-Path $hclPath)) {
        Write-Warning "Template file not found: $hclFile — skipping"
        $FailedTemplates += $tpl
        continue
    }

    $logFile = Join-Path $LogDir "${tpl}_${Timestamp}.log"

    # ---------- Validate ----------
    Write-Host "[$tpl] Validating..." -ForegroundColor Yellow -NoNewline

    $varArgs = @()
    if ($VarFile -and (Test-Path $VarFile)) {
        $varArgs += "-var-file=$VarFile"
    }

    # Packer validate needs the var file AND the template
    $validateArgs = @("validate") + $varArgs + @("-var", "proxmox_password=validate-only", $hclPath)
    $validateResult = & packer @validateArgs 2>&1
    $validateExit = $LASTEXITCODE

    if ($validateExit -ne 0) {
        Write-Host " FAILED" -ForegroundColor Red
        Write-Host "  Validation errors:" -ForegroundColor Red
        $validateResult | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        $FailedTemplates += $tpl
        $validateResult | Out-File -FilePath $logFile -Append -Encoding utf8
        continue
    }

    Write-Host " OK" -ForegroundColor Green

    if ($ValidateOnly) {
        $SuccessTemplates += $tpl
        continue
    }

    # ---------- Build ----------
    Write-Host "[$tpl] Building..." -ForegroundColor Yellow

    $buildArgs = @("build", "-force", "-on-error=cleanup") + $varArgs + @($hclPath)

    Write-Host "  Log: $logFile" -ForegroundColor DarkGray
    & packer @buildArgs 2>&1 | Tee-Object -FilePath $logFile

    if ($LASTEXITCODE -ne 0) {
        Write-Host "[$tpl] Build FAILED (exit code $LASTEXITCODE)" -ForegroundColor Red
        $FailedTemplates += $tpl
    } else {
        Write-Host "[$tpl] Build SUCCESS" -ForegroundColor Green
        $SuccessTemplates += $tpl
    }

    Write-Host ""
}

# ---------- Summary ----------
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Build Summary"                                -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

if ($SuccessTemplates.Count -gt 0) {
    Write-Host " Succeeded: $($SuccessTemplates -join ', ')" -ForegroundColor Green
}
if ($FailedTemplates.Count -gt 0) {
    Write-Host " Failed:    $($FailedTemplates -join ', ')" -ForegroundColor Red
}

Write-Host " Logs:      $LogDir" -ForegroundColor DarkGray
Write-Host ""

if ($FailedTemplates.Count -gt 0) {
    exit 1
}

exit 0