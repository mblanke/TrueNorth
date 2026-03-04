<#
.SYNOPSIS
    TrueNorth Range — Full Platform Backup (PowerShell)
.DESCRIPTION
    Backs up PostgreSQL, Redis, MinIO, OpenSearch, and configuration files.
    Produces a timestamped, checksummed archive suitable for disaster recovery.
.PARAMETER BackupDir
    Root directory for backups (default: ./backups)
.PARAMETER RetentionDays
    Delete backups older than N days (default: 30)
.PARAMETER ComposeFile
    Path to docker-compose file (default: docker-compose.yml)
.EXAMPLE
    .\backup.ps1 -BackupDir "D:\Backups\truenorth" -RetentionDays 14
#>

[CmdletBinding()]
param(
    [string]$BackupDir   = "./backups",
    [int]$RetentionDays  = 30,
    [string]$ComposeFile = "docker-compose.yml",
    [string]$EnvFile     = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ──────────────────────────────────────────────────────────────────
function Write-Log {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffZ"
    Write-Host "[$ts] [$Level] $Message"
}

function Assert-Success {
    param([string]$Step)
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        Write-Log "ERROR" "Step failed: $Step (exit code $LASTEXITCODE)"
        throw "Backup aborted at step: $Step"
    }
}

# ── Setup ────────────────────────────────────────────────────────────────────
$timestamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$backupName  = "truenorth-backup-$timestamp"
$backupPath  = Join-Path $BackupDir $backupName
$manifestFile = Join-Path $backupPath "SHA256SUMS.txt"
$completed   = @()

Write-Log "INFO" "Starting TrueNorth Range backup → $backupPath"
New-Item -ItemType Directory -Path $backupPath -Force | Out-Null

try {
    # ── 1. PostgreSQL ────────────────────────────────────────────────────────
    Write-Log "INFO" "Backing up PostgreSQL..."
    $pgDump = Join-Path $backupPath "postgresql.sql.gz"
    docker exec truenorth-postgres pg_dump -U truenorth -d truenorth_range --clean --if-exists `
        | & {
            param([Parameter(ValueFromPipeline)]$line)
            begin { $sw = [System.IO.StreamWriter]::new($pgDump) }
            process { $sw.WriteLine($line) }
            end { $sw.Close() }
        }
    # Compress
    if (Get-Command gzip -ErrorAction SilentlyContinue) {
        gzip -f $pgDump.Replace(".gz","")
    }
    Assert-Success "PostgreSQL dump"
    $completed += "PostgreSQL"
    Write-Log "INFO" "PostgreSQL backup complete"

    # ── 2. Redis ─────────────────────────────────────────────────────────────
    Write-Log "INFO" "Backing up Redis..."
    docker exec truenorth-redis redis-cli BGSAVE | Out-Null
    Start-Sleep -Seconds 3
    docker cp truenorth-redis:/data/dump.rdb (Join-Path $backupPath "redis-dump.rdb")
    Assert-Success "Redis BGSAVE + copy"
    $completed += "Redis"
    Write-Log "INFO" "Redis backup complete"

    # ── 3. MinIO ─────────────────────────────────────────────────────────────
    Write-Log "INFO" "Backing up MinIO..."
    $minioDir = Join-Path $backupPath "minio"
    New-Item -ItemType Directory -Path $minioDir -Force | Out-Null
    docker run --rm --network host `
        -v "${minioDir}:/backup" `
        minio/mc:latest sh -c "mc alias set src http://minio:9000 minioadmin minioadmin && mc mirror src/ /backup/"
    Assert-Success "MinIO mirror"
    $completed += "MinIO"
    Write-Log "INFO" "MinIO backup complete"

    # ── 4. OpenSearch ────────────────────────────────────────────────────────
    Write-Log "INFO" "Backing up OpenSearch via snapshot API..."
    # Register repo (idempotent)
    $body = '{"type":"fs","settings":{"location":"/mnt/snapshots"}}'
    docker exec truenorth-opensearch curl -s -X PUT "http://localhost:9200/_snapshot/truenorth_backup" `
        -H "Content-Type: application/json" -d $body | Out-Null
    # Create snapshot
    $snapName = "snap-$timestamp"
    docker exec truenorth-opensearch curl -s -X PUT "http://localhost:9200/_snapshot/truenorth_backup/$snapName?wait_for_completion=true" | Out-Null
    # Copy snapshot files out
    $osDir = Join-Path $backupPath "opensearch-snapshots"
    New-Item -ItemType Directory -Path $osDir -Force | Out-Null
    docker cp truenorth-opensearch:/mnt/snapshots/. $osDir
    Assert-Success "OpenSearch snapshot"
    $completed += "OpenSearch"
    Write-Log "INFO" "OpenSearch backup complete"

    # ── 5. Configs ───────────────────────────────────────────────────────────
    Write-Log "INFO" "Backing up configuration files..."
    $cfgDir = Join-Path $backupPath "configs"
    New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null

    $configFiles = @(
        $ComposeFile,
        "docker-compose.dev.yml",
        $EnvFile,
        ".env.example"
    )
    foreach ($f in $configFiles) {
        if (Test-Path $f) {
            Copy-Item $f -Destination $cfgDir -Force
        }
    }

    # Nginx configs
    $nginxSrc = "infra/platform/nginx"
    if (Test-Path $nginxSrc) {
        $nginxDst = Join-Path $cfgDir "nginx"
        Copy-Item $nginxSrc -Destination $nginxDst -Recurse -Force
    }

    # Terraform state
    $tfState = "infra/terraform/terraform.tfstate"
    if (Test-Path $tfState) {
        Copy-Item $tfState -Destination $cfgDir -Force
    }

    $completed += "Configs"
    Write-Log "INFO" "Config backup complete"

    # ── 6. SHA-256 Manifest ──────────────────────────────────────────────────
    Write-Log "INFO" "Generating SHA-256 checksums..."
    $files = Get-ChildItem -Path $backupPath -Recurse -File |
             Where-Object { $_.Name -ne "SHA256SUMS.txt" }
    $checksums = foreach ($f in $files) {
        $hash = (Get-FileHash -Path $f.FullName -Algorithm SHA256).Hash
        $rel  = $f.FullName.Replace("$backupPath\", "").Replace("\", "/")
        "$hash  $rel"
    }
    $checksums | Out-File -FilePath $manifestFile -Encoding UTF8
    Write-Log "INFO" "Manifest written: $manifestFile"

    # ── 7. Retention cleanup ─────────────────────────────────────────────────
    Write-Log "INFO" "Cleaning backups older than $RetentionDays days..."
    $cutoff = (Get-Date).AddDays(-$RetentionDays)
    Get-ChildItem -Path $BackupDir -Directory |
        Where-Object { $_.Name -match "^truenorth-backup-" -and $_.CreationTime -lt $cutoff } |
        ForEach-Object {
            Write-Log "INFO" "Removing old backup: $($_.Name)"
            Remove-Item $_.FullName -Recurse -Force
        }

    # ── Done ─────────────────────────────────────────────────────────────────
    $totalSize = (Get-ChildItem -Path $backupPath -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-Log "INFO" ("Backup complete. Size: {0:N1} MB" -f ($totalSize / 1MB))
    Write-Log "INFO" "Components backed up: $($completed -join ', ')"
    Write-Log "INFO" "Backup location: $backupPath"

} catch {
    Write-Log "ERROR" "Backup FAILED: $_"
    Write-Log "WARN"  "Completed before failure: $($completed -join ', ')"
    exit 1
}