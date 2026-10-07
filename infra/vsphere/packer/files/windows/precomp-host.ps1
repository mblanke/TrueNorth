# TrueNorth derived image: pre-compromised start-state host (clone of win10-22h2).
# Stages benign placeholders for the artifacts an IR/log-analysis start-state needs.
# Real scenario artifacts (specific IOCs, malware, staged creds) are DEPLOY-time (stage P),
# never baked into the golden image.
$ErrorActionPreference = 'Continue'
Write-Host '=== precomp-host role'
New-Item -ItemType Directory -Force -Path 'C:\TrueNorth\precomp' | Out-Null
Set-Content -Path 'C:\TrueNorth\precomp\README.txt' -Value 'Pre-compromised start-state marker. Scenario artifacts injected at deploy (stage P).'
# TODO(deploy): drop scenario-specific staged artifacts, persistence, and log noise here.
