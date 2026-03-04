# =============================================================================
# TrueNorth Range — DNS Module Outputs
# =============================================================================

output "zone_name" {
  description = "Forward DNS zone name for this range."
  value       = local.zone_name
}

output "reverse_zone_name" {
  description = "Reverse DNS zone name for this range."
  value       = local.reverse_zone
}

output "nameserver_ip" {
  description = "IP address of the authoritative nameserver."
  value       = var.nameserver_ip
}

output "record_count" {
  description = "Number of A records provisioned."
  value       = length(var.vm_records)
}