<#
  TrueNorth Range — final Windows provisioner: generalize and shut down.
  Uses an unattend (sysprep-unattend.xml, no product key) so OOBE is skipped on first
  boot of a clone and vSphere guest customization / range deploy can take over.
#>
$ErrorActionPreference = 'Continue'

# ── Nothing from the build stays in the template (#214) ────────────────
# Packer's powershell provisioner writes each provisioner's environment_vars (DEPOT_URL,
# DEPOT_USER, DEPOT_PASSWORD, ...) to C:\Windows\Temp\packer-ps-env-vars-*.ps1 and
# dot-sources it; PowerShell logs script blocks (event 4104, even unprompted for blocks it
# finds "suspicious") and may keep transcripts and PSReadLine history. All of it goes
# before generalizing; if an env-vars file survives, the build fails here.
Get-ChildItem -Path 'C:\Windows\Temp' -Filter 'packer-ps-env-vars-*' -Force -ErrorAction SilentlyContinue |
  Remove-Item -Force -ErrorAction SilentlyContinue
foreach ($log in 'Microsoft-Windows-PowerShell/Operational', 'Windows PowerShell',
                 'PowerShellCore/Operational', 'Microsoft-Windows-WinRM/Operational') {
  & wevtutil.exe cl $log 2>$null
}
$transcriptDirs = @('C:\Users\*\Documents')
$policy = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell\Transcription' -ErrorAction SilentlyContinue
if ($policy -and $policy.OutputDirectory) { $transcriptDirs += $policy.OutputDirectory }
foreach ($dir in $transcriptDirs) {
  Get-ChildItem -Path $dir -Filter 'PowerShell_transcript*' -Recurse -Force -ErrorAction SilentlyContinue |
    Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
}
Get-ChildItem -Path 'C:\Users\*\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine' -Filter '*_history.txt' -Force -ErrorAction SilentlyContinue |
  Remove-Item -Force -ErrorAction SilentlyContinue
$left = @(Get-ChildItem -Path 'C:\Windows\Temp' -Filter 'packer-ps-env-vars-*' -Force -ErrorAction SilentlyContinue)
if ($left.Count -gt 0) {
  Write-Error "sysprep: $($left.Count) Packer env-vars file(s) still in C:\Windows\Temp (they can hold DEPOT_PASSWORD); not generalizing."
  exit 1
}

# This script is Packer's shutdown_command: it must return at once and let Packer wait for the
# power-off. Run synchronously, sysprep powered the VM off under the open WinRM call and Packer
# failed the build ("error sending shutdown command", 2026-10-20).
$unattend = 'C:\Windows\Temp\sysprep-unattend.xml'
$sysprepArgs = @('/oobe', '/generalize', '/shutdown', '/quiet')
if (-not (Test-Path $unattend)) {
  Write-Warning "sysprep-unattend.xml not staged at $unattend; running sysprep without it."
} else {
  $sysprepArgs += "/unattend:$unattend"
}
Start-Process -FilePath "$env:SystemRoot\System32\Sysprep\sysprep.exe" -ArgumentList $sysprepArgs
Write-Host 'sysprep started; it shuts the VM down when generalized.'
exit 0
