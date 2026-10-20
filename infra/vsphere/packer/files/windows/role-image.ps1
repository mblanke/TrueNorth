<#
  TrueNorth Range — Windows Server role images (roles.pkr.hcl). Runs over WinRM as
  Administrator on a customized clone of srv2022, twice, with a reboot in between.

  $env:TN_ROLE_IMAGE  srv2022-exchange2019 | srv2022-sharepointse | srv2022-sharepoint2019 |
                      srv2022-mecm
  $env:TN_ROLE_PASS   1: grow C:, Windows features, depot prerequisites
                      2: post-reboot installs, stage the media, write role-image.json
  $env:DEPOT_URL      TN-DEPOT01 Nexus base (http://10.30.31.30:8081/repository). Vendor
                      prerequisites come from its raw "installers" repository:
                        <DEPOT_URL>/installers/roles/<image>/<file>
                      Required here: a role image without its prerequisites is useless.

  Depot files per image (upload with the tn-prefetch user, README "Role images"):
    exchange2019  vcredist2012_x64.exe, vcredist2013_x64.exe, rewrite_amd64_en-US.msi
                  optional: ndp48-x86-x64-allos-enu.exe (Server 2022 ships .NET 4.8),
                  UcmaRuntimeSetup.exe (else \UCMARedist\Setup.exe from the Exchange ISO)
    sharepointse  VC_redist.x64.exe (Visual C++ 2015-2022)
    sharepoint2019  sqlncli.msi (SQL Server 2012 Native Client), Synchronization.msi (Sync
                  Framework 1.0 SP1 x64), MicrosoftIdentityExtensions-64.msi, setup_msipc_x64.exe
                  (Information Protection and Control Client 2.1), WcfDataServices.exe (WCF Data
                  Services 5.6), vcredist_x64.exe (VC++ 2012), VC_redist.x64.exe (VC++ 2015-2022),
                  WindowsServerAppFabricSetup_x64.exe and AppFabric-KB3092423-x64-ENU.exe (CU7):
                  installed one by one, because SharePoint Server 2019's prerequisite installer
                  does not support Windows Server 2022 (Microsoft Learn, below)
    mecm          adk.zip (adksetup.exe /layout), adkwinpe.zip (adkwinpesetup.exe /layout)
                  optional: msodbcsql18.msi, configmgr-prereqs.zip (setupdl.exe output)

  Domain-dependent steps are NOT run here (they cannot be: the build VM is in a
  workgroup). roles.pkr.hcl lists them; role-image.json in the image repeats them.

  C:\TrueNorth\role-image.json is also the STAGED-IMAGE MARKER: at deploy the worker
  (control-plane/worker/worker/provisioners/vsphere_products.py) runs the domain-dependent
  setup only on a clone whose role-image.json names this image and whose "staged" folder
  exists. Keep its keys: image, staged, sql ("prepared" when SQL Server 2022 was run with
  /ACTION=PrepareImage: MECM's site database, SharePoint's farm database).

  SharePoint Server 2019 on Windows Server 2022: Microsoft Learn, sharepoint/install/
  hardware-and-software-requirements-2019 (supported operating systems, and the list of
  prerequisites to install manually where the prerequisite installer is not supported).
  Verify that page on the first build: it was cited from knowledge, not fetched.

  Sources: Exchange Server prerequisites (Microsoft Learn, exchange/plan-and-deploy/
  prerequisites, Exchange 2019 on Windows Server 2022 Desktop Experience); SQL Server
  SysPrep "Prepare image" / "Complete image" (Microsoft Learn, sql/database-engine/
  install-windows/considerations-for-installing-sql-server-using-sysprep).
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-ExecutionPolicy Bypass -Scope Process -Force

$image = $env:TN_ROLE_IMAGE
$pass = [int]$env:TN_ROLE_PASS
$root = 'C:\TrueNorth'
$dl = Join-Path $root 'depot'
$mediaRoot = Join-Path $root 'media'
New-Item -ItemType Directory -Force -Path $dl, $mediaRoot | Out-Null
function Step($m) { Write-Host "=== $image pass $pass`: $m" }

if ($image -notin 'srv2022-exchange2019', 'srv2022-sharepointse', 'srv2022-sharepoint2019', 'srv2022-mecm') { throw "role-image: unknown TN_ROLE_IMAGE '$image'" }
if ($pass -notin 1, 2) { throw "role-image: TN_ROLE_PASS must be 1 or 2, got '$($env:TN_ROLE_PASS)'" }
if (-not $env:DEPOT_URL) { throw "role-image: DEPOT_URL is empty. Role images install their prerequisites from TN-DEPOT01 (pkrvars-work.py --depot)." }
$installers = "$($env:DEPOT_URL.TrimEnd('/'))/installers/roles/$($image -replace '^srv2022-', '')"

# ── helpers ────────────────────────────────────────────────────────────
function Get-DepotFile([string]$name, [switch]$Optional) {
  $out = Join-Path $dl $name
  if (Test-Path $out) { return $out }
  try {
    Invoke-WebRequest -Uri "$installers/$name" -OutFile $out -UseBasicParsing -TimeoutSec 1800
    return $out
  } catch {
    Remove-Item $out -Force -ErrorAction SilentlyContinue
    if ($Optional) { Write-Warning "role-image: optional $installers/$name not on the depot: $($_.Exception.Message)"; return $null }
    throw "role-image: $installers/$name not on the depot ($($_.Exception.Message)). Upload it to the raw 'installers' repository."
  }
}

function Invoke-Installer([string]$file, [string]$arguments, [int[]]$ok = @(0, 1641, 3010)) {
  Write-Host "  $file $arguments"
  $p = if ($file -like '*.msi') {
    Start-Process msiexec.exe -ArgumentList "/i `"$file`" /qn /norestart $arguments" -Wait -PassThru
  } else {
    Start-Process $file -ArgumentList $arguments -Wait -PassThru
  }
  if ($p.ExitCode -notin $ok) { throw "role-image: $file exited $($p.ExitCode)" }
  return $p.ExitCode
}

# The CD-ROM holding the product media: the first drive with every marker path.
function Find-Media([string[]]$markers, [string[]]$absent = @()) {
  foreach ($d in Get-PSDrive -PSProvider FileSystem) {
    $r = $d.Root
    if (-not ($markers | Where-Object { -not (Test-Path (Join-Path $r $_)) }) -and
        -not ($absent | Where-Object { Test-Path (Join-Path $r $_) })) { return $r }
  }
  return $null
}

function Copy-Media([string]$from, [string]$name) {
  $to = Join-Path $mediaRoot $name
  New-Item -ItemType Directory -Force -Path $to | Out-Null
  Write-Host "  staging $from -> $to"
  & robocopy.exe $from $to /E /R:2 /W:5 /NFL /NDL /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "role-image: robocopy $from -> $to failed ($LASTEXITCODE)" }
  $global:LASTEXITCODE = 0
  return $to
}

# SQL Server 2022 Database Engine as a SysPrep prepared image; CompleteImage runs at deploy.
function Initialize-PreparedSql {
  $sql = Find-Media @('setup.exe', 'x64') @('SMSSETUP', 'sources\install.wim', 'prerequisiteinstaller.exe')
  if (-not $sql) { throw 'role-image: SQL Server 2022 media not found on any CD-ROM' }
  Invoke-Installer (Join-Path $sql 'setup.exe') '/Q /ACTION=PrepareImage /FEATURES=SQLEngine /INSTANCEID=MSSQLSERVER /IACCEPTSQLSERVERLICENSETERMS /SUPPRESSPRIVACYSTATEMENTNOTICE' | Out-Null
}

function Write-RoleManifest([hashtable]$m) {
  $m.image = $image
  $m.built_utc = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
  $m | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $root 'role-image.json')
}

# ── pass 1: disk + features + prerequisites ────────────────────────────
if ($pass -eq 1) {
  Step 'grow C: to the disk size (disk_size in roles.pkr.hcl)'
  $max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
  if ($max -gt (Get-Partition -DriveLetter C).Size + 1GB) { Resize-Partition -DriveLetter C -Size $max }

  switch ($image) {
    'srv2022-exchange2019' {
      Step 'Windows features (Exchange 2019 Mailbox role, Server 2022 Desktop Experience)'
      Install-WindowsFeature Server-Media-Foundation, NET-Framework-45-Core, NET-Framework-45-ASPNET,
        NET-WCF-HTTP-Activation45, NET-WCF-Pipe-Activation45, NET-WCF-TCP-Activation45, NET-WCF-TCP-PortSharing45,
        RPC-over-HTTP-proxy, RSAT-Clustering, RSAT-Clustering-CmdInterface, RSAT-Clustering-Mgmt,
        RSAT-Clustering-PowerShell, WAS-Process-Model, Web-Asp-Net45, Web-Basic-Auth, Web-Client-Auth,
        Web-Digest-Auth, Web-Dir-Browsing, Web-Dyn-Compression, Web-Http-Errors, Web-Http-Logging,
        Web-Http-Redirect, Web-Http-Tracing, Web-ISAPI-Ext, Web-ISAPI-Filter, Web-Metabase, Web-Mgmt-Console,
        Web-Mgmt-Service, Web-Net-Ext45, Web-Request-Monitor, Web-Server, Web-Stat-Compression,
        Web-Static-Content, Web-Windows-Auth, Web-WMI, Windows-Identity-Foundation, RSAT-ADDS | Out-Host

      Step '.NET Framework 4.8 (in-box on Server 2022; depot installer only if older)'
      $release = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction SilentlyContinue).Release
      if ($release -lt 528040) {
        Invoke-Installer (Get-DepotFile 'ndp48-x86-x64-allos-enu.exe') '/q /norestart' | Out-Null
      }

      Step 'Visual C++ 2012 and 2013 redistributables, IIS URL Rewrite (depot)'
      Invoke-Installer (Get-DepotFile 'vcredist2012_x64.exe') '/install /quiet /norestart' | Out-Null
      Invoke-Installer (Get-DepotFile 'vcredist2013_x64.exe') '/install /quiet /norestart' | Out-Null
      Invoke-Installer (Get-DepotFile 'rewrite_amd64_en-US.msi') '' | Out-Null

      Step 'Unified Communications Managed API 4.0 (Exchange ISO \UCMARedist, else depot)'
      $exch = Find-Media @('Setup.exe', 'UCMARedist')
      $ucma = if ($exch) { Join-Path $exch 'UCMARedist\Setup.exe' } else { Get-DepotFile 'UcmaRuntimeSetup.exe' }
      Invoke-Installer $ucma '/passive /norestart' | Out-Null
    }
    'srv2022-sharepointse' {
      Step 'Visual C++ 2015-2022 redistributable (depot), so the prerequisite installer needs no internet'
      Invoke-Installer (Get-DepotFile 'VC_redist.x64.exe') '/install /quiet /norestart' @(0, 1638, 1641, 3010) | Out-Null
      Step 'SharePoint prerequisite installer, first run (Windows features; reboots between runs)'
      $sp = Find-Media @('prerequisiteinstaller.exe', 'setup.exe')
      if (-not $sp) { throw 'role-image: SharePoint media (prerequisiteinstaller.exe) not found on any CD-ROM' }
      # 0 done, 3010 reboot needed, 1001 reboot then run again: pass 2 runs it again.
      Invoke-Installer (Join-Path $sp 'prerequisiteinstaller.exe') '/unattended' @(0, 1001, 3010) | Out-Null
    }
    'srv2022-sharepoint2019' {
      Step '.NET 3.5 and the SharePoint Server 2019 Windows features (Server 2022 ISO sources\sxs)'
      $win = Find-Media @('sources\sxs', 'sources\install.wim')
      if (-not $win) { throw 'role-image: Server 2022 media (sources\sxs) not found on any CD-ROM' }
      Install-WindowsFeature -Source (Join-Path $win 'sources\sxs') NET-Framework-Features, NET-HTTP-Activation,
        NET-Non-HTTP-Activ, NET-WCF-HTTP-Activation45, NET-WCF-Pipe-Activation45, Web-Server, Web-WebServer,
        Web-Common-Http, Web-Static-Content, Web-Default-Doc, Web-Dir-Browsing, Web-Http-Errors, Web-App-Dev,
        Web-Asp-Net, Web-Asp-Net45, Web-Net-Ext, Web-Net-Ext45, Web-ISAPI-Ext, Web-ISAPI-Filter, Web-Health,
        Web-Http-Logging, Web-Log-Libraries, Web-Request-Monitor, Web-Http-Tracing, Web-Security,
        Web-Basic-Auth, Web-Windows-Auth, Web-Filtering, Web-Digest-Auth, Web-Performance,
        Web-Stat-Compression, Web-Dyn-Compression, Web-Mgmt-Tools, Web-Mgmt-Console, WAS, WAS-Process-Model,
        WAS-NET-Environment, WAS-Config-APIs, Windows-Identity-Foundation | Out-Host
      Step 'SharePoint Server 2019 prerequisites, one by one from the depot (no prerequisite installer on Server 2022)'
      Invoke-Installer (Get-DepotFile 'sqlncli.msi') 'IACCEPTSQLNCLILICENSETERMS=YES' | Out-Null
      Invoke-Installer (Get-DepotFile 'Synchronization.msi') '' | Out-Null
      Invoke-Installer (Get-DepotFile 'MicrosoftIdentityExtensions-64.msi') '' | Out-Null
      Invoke-Installer (Get-DepotFile 'setup_msipc_x64.exe') '/quiet /norestart' | Out-Null
      Invoke-Installer (Get-DepotFile 'WcfDataServices.exe') '/quiet /norestart' | Out-Null
      Invoke-Installer (Get-DepotFile 'vcredist_x64.exe') '/install /quiet /norestart' @(0, 1638, 1641, 3010) | Out-Null
      Invoke-Installer (Get-DepotFile 'VC_redist.x64.exe') '/install /quiet /norestart' @(0, 1638, 1641, 3010) | Out-Null
      Invoke-Installer (Get-DepotFile 'WindowsServerAppFabricSetup_x64.exe') '/i CacheClient,CachingService,CacheAdmin /gac' | Out-Null
      Invoke-Installer (Get-DepotFile 'AppFabric-KB3092423-x64-ENU.exe') '/quiet /norestart' | Out-Null
    }
    'srv2022-mecm' {
      Step '.NET 3.5 from the Server 2022 ISO (sources\sxs), then the site-server features'
      $win = Find-Media @('sources\sxs', 'sources\install.wim')
      if (-not $win) { throw 'role-image: Server 2022 media (sources\sxs) not found on any CD-ROM' }
      Install-WindowsFeature NET-Framework-Core -Source (Join-Path $win 'sources\sxs') | Out-Host
      Install-WindowsFeature NET-Framework-45-Features, BITS, BITS-IIS-Ext, RDC, Web-Server, Web-WebServer,
        Web-Common-Http, Web-Default-Doc, Web-Dir-Browsing, Web-Http-Errors, Web-Static-Content,
        Web-Http-Redirect, Web-Health, Web-Http-Logging, Web-Log-Libraries, Web-Request-Monitor,
        Web-Http-Tracing, Web-Performance, Web-Stat-Compression, Web-Security, Web-Filtering,
        Web-Windows-Auth, Web-App-Dev, Web-Net-Ext, Web-Net-Ext45, Web-ISAPI-Ext, Web-ISAPI-Filter,
        Web-Mgmt-Tools, Web-Mgmt-Console, Web-Mgmt-Compat, Web-Metabase, Web-WMI, Web-Scripting-Tools,
        UpdateServices-RSAT | Out-Host
      Step 'ODBC Driver 18 for SQL Server (depot, optional)'
      $odbc = Get-DepotFile 'msodbcsql18.msi' -Optional
      if ($odbc) { Invoke-Installer $odbc 'IACCEPTMSODBCSQLLICENSETERMS=YES' | Out-Null }
    }
  }
  Step 'done; Packer reboots before pass 2'
  exit 0
}

# ── pass 2: post-reboot installs, staging, manifest ────────────────────
switch ($image) {
  'srv2022-exchange2019' {
    $exch = Find-Media @('Setup.exe', 'UCMARedist')
    if (-not $exch) { throw 'role-image: Exchange media (Setup.exe + UCMARedist) not found on any CD-ROM' }
    Step 'stage the Exchange 2019 CU15 media'
    $staged = Copy-Media $exch 'exchange2019'
    Write-RoleManifest @{
      installed = @('Exchange 2019 Mailbox Windows features', 'VC++ 2012', 'VC++ 2013', 'IIS URL Rewrite', 'UCMA 4.0')
      staged = $staged
      sql = 'none'
      deploy_time = @(
        'Join the range domain (Exchange cannot be installed on a workgroup server; TrueNorth, vsphere_products.py).',
        "As an Enterprise + Schema Admin: $staged\Setup.exe /IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF /PrepareSchema, then /PrepareAD /OrganizationName:<org>",
        "$staged\Setup.exe /Mode:Install /Roles:Mailbox /IAcceptExchangeServerLicenseTerms_DiagnosticDataOFF"
      )
    }
  }
  'srv2022-sharepointse' {
    $sp = Find-Media @('prerequisiteinstaller.exe', 'setup.exe')
    if (-not $sp) { throw 'role-image: SharePoint media not found on any CD-ROM' }
    Step 'SharePoint prerequisite installer, second run (must now report 0)'
    $code = Invoke-Installer (Join-Path $sp 'prerequisiteinstaller.exe') '/unattended' @(0, 1001, 3010)
    if ($code -ne 0) { Write-Warning "prerequisiteinstaller returned $code on the second run; SharePoint setup at deploy needs 0: rebuild if setup reports missing prerequisites" }
    Step "SQL Server 2022: SysPrep Prepare Image for the farm's own database (Complete Image runs at deploy)"
    Initialize-PreparedSql
    Step 'stage the SharePoint Subscription Edition media'
    $staged = Copy-Media $sp 'sharepointse'
    Write-RoleManifest @{
      installed = @('VC++ 2015-2022', 'SharePoint prerequisites (prerequisiteinstaller /unattended)',
                    'SQL Server 2022 Database Engine (prepared image, not configured)')
      staged = $staged
      sql = 'prepared'
      deploy_time = @(
        'Join the range domain (TrueNorth, vsphere_products.py).',
        'Complete SQL Server (/ACTION=CompleteImage, collation Latin1_General_CI_AS_KS_WS); the farm uses this instance.',
        "$staged\setup.exe /config <config.xml with the site's PIDKEY: TN_SHAREPOINT_PRODUCT_KEY> (no product key in templates, ADR 0012 decision 3; without it the binaries stay staged)",
        'New-SPConfigurationDatabase -LocalServerRole SingleServerFarm and the psconfig steps (help, security, services, features, Central Administration on 9999)'
      )
    }
  }
  'srv2022-sharepoint2019' {
    $sp = Find-Media @('prerequisiteinstaller.exe', 'setup.exe', 'files\setupfarmsilent')
    if (-not $sp) { throw 'role-image: SharePoint Server 2019 media not found on any CD-ROM' }
    Step "SQL Server 2022: SysPrep Prepare Image for the farm's own database (Complete Image runs at deploy)"
    Initialize-PreparedSql
    Step 'stage the SharePoint Server 2019 media'
    $staged = Copy-Media $sp 'sharepoint2019'
    Write-RoleManifest @{
      installed = @('SharePoint Server 2019 Windows features', 'SharePoint Server 2019 prerequisites (depot, one by one)',
                    'SQL Server 2022 Database Engine (prepared image, not configured)')
      staged = $staged
      sql = 'prepared'
      deploy_time = @(
        'Join the range domain (TrueNorth, vsphere_products.py).',
        'Complete SQL Server (/ACTION=CompleteImage, collation Latin1_General_CI_AS_KS_WS); the farm uses this instance.',
        "$staged\setup.exe /config <config.xml with the site's PIDKEY: TN_SHAREPOINT_PRODUCT_KEY> (no product key in templates, ADR 0012 decision 3; without it the binaries stay staged)",
        'New-SPConfigurationDatabase -LocalServerRole SingleServerFarm and the psconfig steps (help, security, services, features, Central Administration on 9999)'
      )
    }
  }
  'srv2022-mecm' {
    Step 'Windows ADK (Deployment Tools, USMT) and the WinPE add-on from the depot layouts'
    foreach ($pkg in @(
        @{ zip = 'adk.zip'; exe = 'adksetup.exe'; features = 'OptionId.DeploymentTools OptionId.UserStateMigrationTool' },
        @{ zip = 'adkwinpe.zip'; exe = 'adkwinpesetup.exe'; features = 'OptionId.WindowsPreinstallationEnvironment' })) {
      $dir = Join-Path $dl ($pkg.zip -replace '\.zip$', '')
      Expand-Archive -Path (Get-DepotFile $pkg.zip) -DestinationPath $dir -Force
      $exe = Get-ChildItem -Path $dir -Recurse -Filter $pkg.exe | Select-Object -First 1
      if (-not $exe) { throw "role-image: $($pkg.exe) not in $($pkg.zip)" }
      Invoke-Installer $exe.FullName "/quiet /norestart /ceip off /features $($pkg.features)" | Out-Null
    }

    Step 'SQL Server 2022: SysPrep Prepare Image (Database Engine); Complete Image runs at deploy'
    Initialize-PreparedSql

    Step 'stage the ConfigMgr media (and setupdl prerequisites when the depot has them)'
    $cm = Find-Media @('SMSSETUP\BIN\X64\setup.exe')
    if (-not $cm) { throw 'role-image: ConfigMgr media (SMSSETUP\BIN\X64\setup.exe) not found on any CD-ROM' }
    $staged = Copy-Media $cm 'mecm'
    $prereqs = Get-DepotFile 'configmgr-prereqs.zip' -Optional
    if ($prereqs) { Expand-Archive -Path $prereqs -DestinationPath (Join-Path $mediaRoot 'mecm-prereqs') -Force }
    Write-RoleManifest @{
      installed = @('.NET 3.5 + 4.8', 'BITS/IIS/RDC site-server features', 'Windows ADK + WinPE add-on',
                    'SQL Server 2022 Database Engine (prepared image, not configured)')
      staged = $staged
      sql = 'prepared'
      deploy_time = @(
        'Join the range domain (TrueNorth, vsphere_products.py).',
        "Complete SQL (the prepared instance's Start-menu 'Complete' shortcut, or its local Setup Bootstrap setup.exe): /Q /ACTION=CompleteImage /INSTANCENAME=MSSQLSERVER /INSTANCEID=MSSQLSERVER /SQLSYSADMINACCOUNTS=<domain admins> /SQLCOLLATION=SQL_Latin1_General_CP1_CI_AS /IACCEPTSQLSERVERLICENSETERMS (product key: the media's default or the site's)",
        "Extend the AD schema ($staged\SMSSETUP\BIN\X64\extadsch.exe) and create CN=System Management with rights for this computer",
        "$staged\SMSSETUP\BIN\X64\setup.exe /SCRIPT <ini> (prerequisites: $mediaRoot\mecm-prereqs when staged)"
      )
    }
  }
}
Remove-Item -Recurse -Force $dl -ErrorAction SilentlyContinue
Step "done: $(Join-Path $root 'role-image.json')"
