<#
  Variant script (win10-office): install Microsoft Office LTSC from TN-DEPOT01, if present.

  Office is not on the community Chocolatey feed and needs volume-licence media, so it is
  staged on the depot by hand as ONE zip:

    $DEPOT_URL/office/office-ltsc.zip
      setup.exe            Office Deployment Tool (ODT) setup.exe
      configuration.xml    ODT config: Product ID (e.g. ProPlus2024Volume), PIDKEY (the
                           public GVLK; activation is KMS, deferred), Display Level="None",
                           AcceptEULA="TRUE", and NO SourcePath, so ODT installs from
                           this folder
      Office\Data\...      the payload from `setup.exe /download configuration.xml`

  No depot, or no zip on it: log a warning and return success, so the rest of the variant
  still builds (Office is then simply absent). A zip that is present but fails to install
  fails the build.
#>
$ErrorActionPreference = 'Stop'

$depot = $env:DEPOT_URL
if (-not $depot) {
  Write-Warning 'office-ltsc: DEPOT_URL not set; Office LTSC NOT installed. Stage office/office-ltsc.zip on the depot and rebuild.'
  exit 0
}

$url = "$depot/office/office-ltsc.zip"
try {
  Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing -TimeoutSec 15 | Out-Null
} catch {
  Write-Warning "office-ltsc: $url not found on the depot; Office LTSC NOT installed."
  exit 0
}

$work = 'C:\Windows\Temp\office-ltsc'
New-Item -ItemType Directory -Force -Path $work | Out-Null
$zip = Join-Path $work 'office-ltsc.zip'
Write-Host "office-ltsc: downloading $url"
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $work -Force

$setup = Join-Path $work 'setup.exe'
$config = Join-Path $work 'configuration.xml'
foreach ($f in $setup, $config) {
  if (-not (Test-Path $f)) { throw "office-ltsc: $f missing from office-ltsc.zip" }
}

Write-Host 'office-ltsc: running ODT /configure'
$p = Start-Process -FilePath $setup -ArgumentList "/configure `"$config`"" -WorkingDirectory $work -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "office-ltsc: ODT exited with $($p.ExitCode)" }

Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
Write-Host 'office-ltsc: installed'
