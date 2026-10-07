<#
  TrueNorth Range — Windows variant packages (variants.pkr.hcl, variant-windows).
  Runs over WinRM on a customized clone of a Windows base, before the variant scripts.

  $env:VARIANT_CHOCO_PACKAGES  ';'-separated Chocolatey ids; "id@1.2.3" pins a version
  $env:DEPOT_URL               TN-DEPOT01 base URL; when set, its /chocolatey feed is the
                               only source. Else the community feed (the build network
                               has egress).

  Unlike setup.ps1, a variant fails the build when it cannot install a package: a variant
  that silently lacks its software would only be noticed inside a no-egress range.
#>
$ErrorActionPreference = 'Stop'
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072

Write-Host "=== variant $($env:VARIANT_NAME): packages"
$packages = @(($env:VARIANT_CHOCO_PACKAGES -split ';') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($packages.Count -eq 0) {
  Write-Host 'No Chocolatey packages requested.'
  exit 0
}

$depot = $env:DEPOT_URL
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
  $installer = if ($depot) { "$depot/chocolatey/install.ps1" } else { 'https://community.chocolatey.org/install.ps1' }
  Write-Host "Bootstrapping Chocolatey from $installer"
  Invoke-Expression ((New-Object System.Net.WebClient).DownloadString($installer))
  $env:Path += ";$env:ProgramData\chocolatey\bin"
}
if ($depot) {
  choco source add -n truenorth -s "$depot/chocolatey" --priority 1 | Out-Null
  choco source disable -n chocolatey | Out-Null
  Write-Host "Using depot feed $depot/chocolatey only."
}

# Native commands from here on: in Windows PowerShell 5.1, 'Stop' turns any stderr line
# of `choco ... 2>&1` into a terminating error. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$failed = @()
foreach ($spec in $packages) {
  $id, $version = $spec -split '@', 2
  $chocoArgs = @('install', '-y', '--no-progress', $id)
  if ($version) { $chocoArgs += @('--version', $version) }
  Write-Host "choco $($chocoArgs -join ' ')"
  & choco @chocoArgs 2>&1 | Out-Host
  # 0 ok; 1641/3010 ok, reboot required (taken by the windows-restart provisioner).
  if ($LASTEXITCODE -notin 0, 1641, 3010) {
    Write-Warning "choco install $spec failed with exit code $LASTEXITCODE"
    $failed += $spec
  }
}

# Keep the template small: the package cache is not needed in a range.
choco cache remove -y 2>&1 | Out-Null
Remove-Item -Recurse -Force "$env:TEMP\chocolatey" -ErrorAction SilentlyContinue

if ($failed.Count -gt 0) {
  throw "Variant $($env:VARIANT_NAME) is missing packages: $($failed -join ', ')"
}
Write-Host "=== variant $($env:VARIANT_NAME): $($packages.Count) packages installed"
