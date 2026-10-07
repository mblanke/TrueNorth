<#
  TrueNorth Range — Windows template baseline (stage T).
  Runs over WinRM during the Packer build, after Windows is installed.

  - Installs VMware Tools silently from the mounted Tools ISO (any CD drive).
  - Baseline apps via Chocolatey, from the depot feed when $env:DEPOT_URL is set,
    else from the community feed only if the internet is reachable, else skipped
    gracefully (no-egress builds stay green).
  - Power / hibernation tuning.
  Sysprep is a separate, final Packer provisioner (sysprep.ps1) so this script can be
  re-run during development without generalizing the image.
#>
$ErrorActionPreference = 'Stop'
Set-ExecutionPolicy Bypass -Scope Process -Force

function Write-Step($m) { Write-Host "=== $m" }

# --- VMware Tools (silent) from the mounted Tools ISO --------------------------------
Write-Step 'VMware Tools'
$toolsSetup = Get-ChildItem -Path (Get-PSDrive -PSProvider FileSystem | ForEach-Object { "$($_.Root)setup64.exe" }) -ErrorAction SilentlyContinue |
  Select-Object -First 1
if ($toolsSetup) {
  Write-Host "Installing VMware Tools from $($toolsSetup.FullName)"
  Start-Process -FilePath $toolsSetup.FullName -ArgumentList '/S /v "/qn REBOOT=R"' -Wait
} else {
  Write-Warning 'VMware Tools setup64.exe not found on any CD drive; ensure the Tools ISO is attached (vmtools_iso_path). Guest customization needs Tools.'
}

# --- WinRM already enabled by Autounattend; make sure OpenSSH server is present -------
Write-Step 'OpenSSH Server'
try {
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null
  Set-Service sshd -StartupType Automatic
  Start-Service sshd -ErrorAction SilentlyContinue
} catch { Write-Warning "OpenSSH server not installed (offline capability source): $_" }

# --- Chocolatey baseline (§5.1) ------------------------------------------------------
Write-Step 'Chocolatey baseline'
$depot = $env:DEPOT_URL
$baseline = @('7zip','notepadplusplus','powershell-core','vcredist140','googlechrome','firefoxesr','adobereader','sysmon')

function Test-Online {
  try { (Invoke-WebRequest -Uri 'https://community.chocolatey.org' -UseBasicParsing -TimeoutSec 8).StatusCode -eq 200 }
  catch { $false }
}

$canInstall = $false
if ($depot) {
  Write-Host "Using depot Chocolatey feed: $depot/chocolatey"
  if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    # Bootstrap choco from the depot (install.ps1 mirrored under the depot).
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString("$depot/chocolatey/install.ps1"))
  }
  choco source add -n truenorth -s "$depot/chocolatey" --priority 1 | Out-Null
  choco source disable -n chocolatey | Out-Null
  $canInstall = $true
} elseif (Test-Online) {
  Write-Host 'Depot not set but internet reachable; using the community feed.'
  if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
  }
  $canInstall = $true
} else {
  Write-Warning 'No depot_url and no internet; skipping the baseline app install (template still valid; apps land at deploy via Ansible).'
}

if ($canInstall) {
  foreach ($pkg in $baseline) {
    Write-Host "choco install $pkg"
    choco install -y --no-progress $pkg 2>&1 | Out-Host
  }
}

# --- Power / hibernation -------------------------------------------------------------
Write-Step 'Power plan'
powercfg /hibernate off 2>$null
powercfg /setactive SCHEME_MIN 2>$null   # High performance

Write-Step 'Baseline done'
