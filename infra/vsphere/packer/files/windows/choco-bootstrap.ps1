<#
  TrueNorth Range — install Chocolatey from the TN-DEPOT01 feed (no internet, no install.ps1).

  Nexus serves the Chocolatey group feed at $env:DEPOT_URL/chocolatey (NuGet v2), but not
  community.chocolatey.org's install.ps1. That script only downloads the chocolatey .nupkg,
  unzips it and runs tools\chocolateyInstall.ps1; this does the same against the depot:

    1. version: $env:CHOCOLATEY_VERSION, else the newest stable "chocolatey" in the feed
       (FindPackagesById, following the feed's paging links);
    2. download <feed>/chocolatey/<version> (Nexus' NuGet v2 package path);
    3. unzip, run tools\chocolateyInstall.ps1, keep the .nupkg under lib\chocolatey.

  This is Chocolatey's documented offline install ("Completely offline install",
  https://docs.chocolatey.org/en-us/choco/setup/). The depot must hold the package: it is
  in the prefetch list (install/lab/roles/tn_prefetch, tn_prefetch_choco_extra), or the
  chocolatey-proxy cached it during an online window.

  No DEPOT_URL, or Chocolatey already installed: nothing to do (exit 0). A DEPOT_URL whose
  feed has no chocolatey package fails the build: every later step would silently skip.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-ExecutionPolicy Bypass -Scope Process -Force

$chocoExe = Join-Path $env:ProgramData 'chocolatey\bin\choco.exe'
if ((Get-Command choco -ErrorAction SilentlyContinue) -or (Test-Path $chocoExe)) {
  Write-Host 'choco-bootstrap: Chocolatey already installed'
  exit 0
}
$depot = $env:DEPOT_URL
if (-not $depot) {
  Write-Host 'choco-bootstrap: no DEPOT_URL; nothing to do (setup.ps1 decides about the community feed)'
  exit 0
}
$feed = "$($depot.TrimEnd('/'))/chocolatey"

function Get-Text($node) {
  if ($null -eq $node) { return '' }
  if ($node -is [System.Xml.XmlNode]) { return $node.InnerText }
  return [string]$node
}

$version = $env:CHOCOLATEY_VERSION
if (-not $version) {
  $url = "$feed/FindPackagesById()?id='chocolatey'"
  $found = @()
  for ($page = 0; $url -and $page -lt 50; $page++) {
    [xml]$doc = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 120).Content
    foreach ($e in @($doc.feed.entry)) {
      if ($null -eq $e) { continue }
      $v = Get-Text $e.properties.Version
      $pre = (Get-Text $e.properties.IsPrerelease) -eq 'true'
      $parsed = $null
      if ($v -and -not $pre -and [version]::TryParse($v, [ref]$parsed)) { $found += [pscustomobject]@{ v = $parsed; s = $v } }
    }
    $next = @($doc.feed.link) | Where-Object { $_.rel -eq 'next' } | Select-Object -First 1
    $url = if ($next) { $next.href } else { $null }
  }
  if ($found.Count -eq 0) {
    throw "choco-bootstrap: $feed has no stable 'chocolatey' package. Cache it in the depot (prefetch, or an online window) or set chocolatey_version."
  }
  $version = ($found | Sort-Object v -Descending | Select-Object -First 1).s
}

$work = Join-Path $env:TEMP 'tn-choco-bootstrap'
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $work | Out-Null
$zip = Join-Path $work 'chocolatey.zip'   # Expand-Archive (PS 5.1) wants a .zip name
Write-Host "choco-bootstrap: chocolatey $version from $feed"
Invoke-WebRequest -Uri "$feed/chocolatey/$version" -OutFile $zip -UseBasicParsing -TimeoutSec 600
Expand-Archive -Path $zip -DestinationPath (Join-Path $work 'pkg') -Force

$installer = Join-Path $work 'pkg\tools\chocolateyInstall.ps1'
if (-not (Test-Path $installer)) { throw "choco-bootstrap: $installer missing: not a chocolatey package" }
& $installer

$chocoHome = [Environment]::GetEnvironmentVariable('ChocolateyInstall', 'Machine')
if (-not $chocoHome) { $chocoHome = Join-Path $env:ProgramData 'chocolatey' }
$lib = Join-Path $chocoHome 'lib\chocolatey'
New-Item -ItemType Directory -Force -Path $lib | Out-Null
Copy-Item $zip (Join-Path $lib 'chocolatey.nupkg') -Force
if (-not (Test-Path (Join-Path $chocoHome 'bin\choco.exe'))) { throw 'choco-bootstrap: choco.exe not found after install' }
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
Write-Host "choco-bootstrap: Chocolatey $version installed in $chocoHome"
