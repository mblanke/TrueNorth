# =============================================================================
# TrueNorth Range — DNS Module Variables
# =============================================================================

variable "range_id" {
  description = "Unique identifier for the cyber range instance."
  type        = string
}

variable "range_domain" {
  description = "DNS domain for this range (e.g. range-42.tn.local)."
  type        = string
}

variable "vm_records" {
  description = "List of VM DNS records: [{name, ip}]."
  type = list(object({
    name = string
    ip   = string
  }))
  default = []
}

variable "upstream_dns" {
  description = "Upstream DNS server for external resolution forwarding."
  type        = string
  default     = "1.1.1.1"
}

variable "nameserver_ip" {
  description = "IP address of the local DNS server (CoreDNS / dnsmasq)."
  type        = string
  default     = "10.0.10.2"
}

variable "dns_ssh_user" {
  description = "SSH user for the DNS server."
  type        = string
  default     = "root"
}

variable "dns_ssh_key" {
  description = "SSH private key content for DNS server access."
  type        = string
  sensitive   = true
}