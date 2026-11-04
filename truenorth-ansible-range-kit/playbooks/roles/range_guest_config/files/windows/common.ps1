# TrueNorth common Windows guest configuration. Runs through VMware Tools.
# Inputs (environment):
#   TN_HOSTNAME, TN_ROLE, TN_INSTANCE, TN_NIC_COUNT
#   TN_NIC_<n>="<mac>|<ip>/<prefix>|<gateway or empty>"
#   TN_VAR_<KEY>=<value>
# Matches adapters by MAC address. The playbook reboots the guest afterwards.
# LAB-VALIDATE on each template before relying on it.
$ErrorActionPreference = 'Stop'

function Log($m) { Write-Output "[tn-common] $m" }

if (-not $env:TN_HOSTNAME) { Write-Error 'TN_HOSTNAME missing'; exit 10 }
$count = [int]$env:TN_NIC_COUNT

for ($i = 0; $i -lt $count; $i++) {
    $raw = [Environment]::GetEnvironmentVariable("TN_NIC_$i")
    $mac, $addr, $gw = $raw.Split('|')
    $ip, $prefix = $addr.Split('/')
    $macDash = ($mac.ToUpper() -replace ':', '-')
    $nic = Get-NetAdapter | Where-Object { $_.MacAddress -eq $macDash }
    if (-not $nic) { Write-Error "no adapter for $mac"; exit 11 }

    Rename-NetAdapter -Name $nic.Name -NewName "tn$i" -ErrorAction SilentlyContinue
    $nic = Get-NetAdapter | Where-Object { $_.MacAddress -eq $macDash }
    Set-NetIPInterface -InterfaceIndex $nic.ifIndex -Dhcp Disabled
    Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false
    Get-NetRoute -InterfaceIndex $nic.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false
    $params = @{ InterfaceIndex = $nic.ifIndex; IPAddress = $ip; PrefixLength = [int]$prefix }
    if ($gw) { $params.DefaultGateway = $gw }
    New-NetIPAddress @params | Out-Null
    if ($env:TN_VAR_DNS) {
        Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses $env:TN_VAR_DNS.Split(';')
    }
    Disable-NetAdapterBinding -Name "tn$i" -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue
    Log "tn$i $mac -> $addr gw '$gw'"
}

if ($env:COMPUTERNAME -ne $env:TN_HOSTNAME.ToUpper()) {
    Rename-Computer -NewName $env:TN_HOSTNAME -Force
    Log "renamed to $($env:TN_HOSTNAME) (takes effect after reboot)"
}
Log "configured $($env:TN_HOSTNAME) ($($env:TN_ROLE)) with $count NIC(s)"
exit 0
