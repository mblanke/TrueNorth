# TrueNorth derived image: sterile malware detonation host (clone of win10-22h2).
# sensor_baked=NO and NO egress ever. This script must NOT install any telemetry agent.
$ErrorActionPreference = 'Continue'
Write-Host '=== detonation-host role (sterile; no sensor, no egress)'
New-Item -ItemType Directory -Force -Path 'C:\TrueNorth\detonation' | Out-Null
Set-Content -Path 'C:\TrueNorth\detonation\README.txt' -Value 'Sterile detonation host. No sensor. Snapshot-revert after each use. No egress.'
# Defender is left as-is here with a comment rather than changed blindly: whether to disable
# real-time protection depends on the analysis technique. Decide per ALRA SOP at deploy time.
# TODO(deploy): set Defender posture per SOP; confirm the range segment has zero egress.
