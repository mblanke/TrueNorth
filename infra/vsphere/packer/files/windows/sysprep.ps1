<#
  TrueNorth Range — final Windows step: scrub build secrets and shut down. No generalize.
  The template stays specialized on purpose: vSphere guest customization (the worker's
  windows_customization(), scripts/lab/test-clone.sh) runs Sysprep itself on every clone.
  A template generalized here as well cannot be customized (2026-10-21, win11-24h2).
#>
$ErrorActionPreference = 'Continue'

# ── Nothing from the build stays in the template (#214) ────────────────
# Packer's powershell provisioner writes each provisioner's environment_vars (DEPOT_URL,
# DEPOT_USER, DEPOT_PASSWORD, ...) to C:\Windows\Temp\packer-ps-env-vars-*.ps1 and
# dot-sources it; PowerShell logs script blocks (event 4104, even unprompted for blocks it
# finds "suspicious") and may keep transcripts and PSReadLine history. All of it goes
# before shutdown; if an env-vars file survives, the build fails here.
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
  Write-Error "sysprep: $($left.Count) Packer env-vars file(s) still in C:\Windows\Temp (they can hold DEPOT_PASSWORD); not shutting down."
  exit 1
}

# This script is Packer's shutdown_command: it must return at once and let Packer wait for the
# power-off. A shutdown run synchronously powers the VM off under the open WinRM call and
# Packer fails the build ("error sending shutdown command", 2026-10-20).
# Not Start-Process: WinRM runs each command in a job object and kills every process it started
# when the command returns (win11, 2026-10-20). A one-off SYSTEM scheduled task runs outside it.
# The task removes itself before the VM goes down, so no template (and no clone) carries a
# SYSTEM task that shuts the machine down: shutdown.exe only schedules the shutdown (/t 5)
# and returns, then schtasks deletes the task's registration while it still can.
$task = 'TN-Shutdown-Once'
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
  -Argument "/c `"$env:SystemRoot\System32\shutdown.exe /s /t 5 /f & $env:SystemRoot\System32\schtasks.exe /Delete /TN $task /F`""
Register-ScheduledTask -TaskName $task -Action $action -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName $task
Write-Host "shutdown started (scheduled task $task); not generalized, vSphere customization runs Sysprep on clone."
exit 0
