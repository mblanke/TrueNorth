<#
.SYNOPSIS
    TrueNorth Range — Full Platform Restore (PowerShell)
.DESCRIPTION
    Restores PostgreSQL, Redis, MinIO, OpenSearch, and configs from a backup
    created by backup.ps1.
.PARAMETER BackupPath
    Path to the backup directory (e.g. ./backups/truenorth-backup-20260225-020000)
.PARAMETER Force
    Skip confirmation prompt
.EXAMPLE
    .\restore.ps1 -BackupPath "./backups/truenorth-backup-20260225-020000" -Force
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ──────────────────────────────────────────────────────────────────
function Write-Log {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffZ"
    Write-Host "[$ts] [$Level] $Message"
}

# ── Validate backup directory ────────────────────────────────────────────────
if (-not (Test-Path $BackupPath)) {
    Write-Log "ERROR" "Backup path not found: $BackupPath"
    exit 1
}

$manifestFile = Join-Path $BackupPath "SHA256SUMS.txt"
if (-not (Test-Path $manifestFile)) {
    Write-Log "ERROR" "SHA256SUMS.txt not found in backup — cannot verify integrity"
    exit 1
}

# ── Verify checksums ────────────────────────────────────────────────────────
Write-Log "INFO" "Verifying SHA-256 checksums..."
$failures = 0
Get-Content $manifestFile | ForEach-Object {
    $parts    = $_ -split '  ', 2
    $expected = $parts[0]
    $relPath  = $parts[1]
    $fullPath = Join-Path $BackupPath $relPath.Replace("/", "\")

    if (-not (Test-Path $fullPath)) {
        Write-Log "ERROR" "Missing file: $relPath"
        $failures++
        return
    }
    $actual = (Get-FileHash -Path $fullPath -Algorithm SHA256).Hash
    if ($actual -ne $expected) {
        Write-Log "ERROR" "Checksum mismatch: $relPath"
        $failures++
    }
}
if ($failures -gt 0) {
    Write-Log "ERROR" "$failures checksum verification failure(s). Aborting."
    exit 1
}
Write-Log "INFO" "All checksums verified successfully"

# ── Confirmation ─────────────────────────────────────────────────────────────
if (-not $Force) {
    Write-Host ""
    Write-Host "WARNING: This will OVERWRITE current data with backup from:" -ForegroundColor Yellow
    Write-Host "  $BackupPath" -ForegroundColor Cyan
    Write-Host ""
    $answer = Read-Host "Type 'YES' to proceed"
    if ($answer -ne "YES") {
        Write-Log "INFO" "Restore cancelled by user"
        exit 0
    }
}

$completed = @()

try {
    # ── 1. PostgreSQL ────────────────────────────────────────────────────────
    $pgDump = Join-Path $BackupPath "postgresql.sql.gz"
    if (Test-Path $pgDump) {
        Write-Log "INFO" "Restoring PostgreSQL..."
        if (Get-Command gunzip -ErrorAction SilentlyContinue) {
            $pgSql = $pgDump.Replace(".gz", "")
            gunzip -k -f $pgDump
            Get-Content $pgSql | docker exec -i truenorth-postgres psql -U truenorth -d truenorth_range
            Remove-Item $pgSql -Force -ErrorAction SilentlyContinue
        } else {
            # Try using docker to decompress
            docker exec -i truenorth-postgres sh -c "gunzip | psql -U truenorth -d truenorth_range" < $pgDump
        }
        $completed += "PostgreSQL"
        Write-Log "INFO" "PostgreSQL restore complete"
    } else {
        Write-Log "WARN" "No PostgreSQL dump found — skipping"
    }

    # ── 2. Redis ─────────────────────────────────────────────────────────────
    $redisDump = Join-Path $BackupPath "redis-dump.rdb"
    if (Test-Path $redisDump) {
        Write-Log "INFO" "Restoring Redis..."
        docker stop truenorth-redis 2>$null
        docker cp $redisDump truenorth-redis:/data/dump.rdb
        docker start truenorth-redis
        Start-Sleep -Seconds 3
        $completed += "Redis"
        Write-Log "INFO" "Redis restore complete"
    } else {
        Write-Log "WARN" "No Redis dump found — skipping"
    }

    # ── 3. MinIO ─────────────────────────────────────────────────────────────
    $minioDir = Join-Path $BackupPath "minio"
    if (Test-Path $minioDir) {
        Write-Log "INFO" "Restoring MinIO..."
        docker run --rm --network host `
            -v "${minioDir}:/backup" `
            minio/mc:latest sh -c "mc alias set dst http://minio:9000 minioadmin minioadmin && mc mirror /backup/ dst/"
        $completed += "MinIO"
        Write-Log "INFO" "MinIO restore complete"
    } else {
        Write-Log "WARN" "No MinIO backup found — skipping"
    }

    # ── 4. OpenSearch ────────────────────────────────────────────────────────
    $osDir = Join-Path $BackupPath "opensearch-snapshots"
    if (Test-Path $osDir) {
        Write-Log "INFO" "Restoring OpenSearch..."
        docker cp "${osDir}/." truenorth-opensearch:/mnt/snapshots/
        # Register repo
        $body = '{"type":"fs","settings":{"location":"/mnt/snapshots"}}'
        docker exec truenorth-opensearch curl -s -X PUT `
            "http://localhost:9200/_snapshot/truenorth_backup" `
            -H "Content-Type: application/json" -d $body | Out-Null

        # Find latest snapshot
        $snapshots = docker exec truenorth-opensearch curl -s `
            "http://localhost:9200/_snapshot/truenorth_backup/_all" | ConvertFrom-Json
        $latestSnap = ($snapshots.snapshots | Sort-Object start_time_in_millis | Select-Object -Last 1).snapshot

        if ($latestSnap) {
            # Close all indices before restore
            docker exec truenorth-opensearch curl -s -X POST "http://localhost:9200/_all/_close" | Out-Null
            docker exec truenorth-opensearch curl -s -X POST `
                "http://localhost:9200/_snapshot/truenorth_backup/${latestSnap}/_restore?wait_for_completion=true" | Out-Null
            $completed += "OpenSearch"
            Write-Log "INFO" "OpenSearch restore complete (snapshot: $latestSnap)"
        } else {
            Write-Log "WARN" "No snapshots found in backup — skipping OpenSearch restore"
        }
    } else {
        Write-Log "WARN" "No OpenSearch backup found — skipping"
    }

    # ── 5. Run Alembic migrations ────────────────────────────────────────────
    Write-Log "INFO" "Running Alembic migrations..."
    docker exec truenorth-api alembic upgrade head
    $completed += "Alembic"
    Write-Log "INFO" "Alembic migrations complete"

    # ── 6. Health check ──────────────────────────────────────────────────────
    Write-Log "INFO" "Running post-restore health check..."
    $maxRetries = 10
    $healthy = $false
    for ($i = 1; $i -le $maxRetries; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:8080/health" -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {}
        Write-Log "INFO" "Health check attempt $i/$maxRetries — waiting..."
        Start-Sleep -Seconds 5
    }

    if ($healthy) {
        Write-Log "INFO" "Health check PASSED"
    } else {
        Write-Log "WARN" "Health check did not pass after $maxRetries attempts — manual verification required"
    }

    # ── Done ─────────────────────────────────────────────────────────────────
    Write-Log "INFO" "Restore complete. Components restored: $($completed -join ', ')"

} catch {
    Write-Log "ERROR" "Restore FAILED: $_"
    Write-Log "WARN"  "Completed before failure: $($completed -join ', ')"
    exit 1
}