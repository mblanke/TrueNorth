# =============================================================================
# TrueNorth Range — Networking Module Outputs
# =============================================================================

output "vlan_id" {
  description = "The 802.1Q VLAN tag assigned to this range."
  value       = var.range_vlan_id
}

output "gateway_ip" {
  description = "Gateway IP address for the range subnet."
  value       = local.gateway
}

output "network_cidr" {
  description = "Range subnet in CIDR notation."
  value       = local.network
}

output "dhcp_range" {
  description = "DHCP address range (start–end)."
  value       = "${local.dhcp_start}-${local.dhcp_end}"
}

output "bridge_name" {
  description = "Name of the Linux bridge created for this range."
  value       = local.bridge_name
}

output "firewall_rule_ids" {
  description = "IDs of the Proxmox firewall rules created for this range."
  value = concat(
    [for r in proxmox_virtual_environment_cluster_firewall_security_group.range_sg : r.name],
  )
}

output "range_ipset_name" {
  description = "IPSet name containing the range subnet."
  value       = proxmox_virtual_environment_firewall_ipset.range_subnet.name
}