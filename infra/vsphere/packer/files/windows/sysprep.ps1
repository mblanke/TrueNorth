<#
  TrueNorth Range — final Windows provisioner: generalize and shut down.
  Uses an unattend (sysprep-unattend.xml, no product key) so OOBE is skipped on first
  boot of a clone and vSphere guest customization / range deploy can take over.
#>
$ErrorActionPreference = 'Continue'
$unattend = 'C:\Windows\Temp\sysprep-unattend.xml'
if (-not (Test-Path $unattend)) {
  Write-Warning "sysprep-unattend.xml not staged at $unattend; running sysprep without it."
  & "$env:SystemRoot\System32\Sysprep\sysprep.exe" /oobe /generalize /shutdown /quiet
} else {
  & "$env:SystemRoot\System32\Sysprep\sysprep.exe" /oobe /generalize /shutdown /quiet /unattend:$unattend
}
