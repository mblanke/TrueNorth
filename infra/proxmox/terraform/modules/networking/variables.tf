# =============================================================================
# TrueNorth Range — Networking Module Variables
# =============================================================================

# ---- Identity -------------------------------------------------------------

variable "range_id" {
  description = "Unique identifier for the cyber range instance."
  type        = string
}

variable "range_vlan_id" {
  description = "802.1Q VLAN tag for this range (100-4000)."
  type        = number

  validation {
    condition     = var.range_vlan_id >= 100 && var.range_vlan_id <= 4000
    error_message = "range_vlan_id must be between 100 and 4000."
  }
}

# ---- Network topology -----------------------------------------------------

variable "mgmt_subnet" {
  description = "Management VLAN subnet (VLAN 10)."
  type        = string
  default     = "10.0.10.0/24"
}

variable "monitoring_subnet" {
  description = "Monitoring VLAN subnet (VLAN 20)."
  type        = string
  default     = "10.0.20.0/24"
}

variable "internet_access" {
  description = "Allow range VMs to reach the Internet via NAT."
  type        = bool
  default     = false
}

variable "allowed_ports" {
  description = "List of TCP ports allowed from range to Internet when internet_access=true."
  type        = list(number)
  default     = [80, 443, 53]
}

# ---- DHCP -----------------------------------------------------------------

variable "dhcp_enabled" {
  description = "Provision a DHCP scope for this range VLAN."
  type        = bool
  default     = true
}

variable "dhcp_start_offset" {
  description = "Last octet of the DHCP range start address."
  type        = number
  default     = 100
}

variable "dhcp_end_offset" {
  description = "Last octet of the DHCP range end address."
  type        = number
  default     = 250
}

# ---- DNS ------------------------------------------------------------------

variable "dns_server" {
  description = "IP address of the internal DNS server (CoreDNS / dnsmasq)."
  type        = string
  default     = "10.0.10.2"
}

variable "upstream_dns" {
  description = "Upstream DNS forwarder (e.g. 1.1.1.1)."
  type        = string
  default     = "1.1.1.1"
}

# ---- Proxmox infrastructure -----------------------------------------------

variable "proxmox_nodes" {
  description = "List of Proxmox node names to provision the bridge on."
  type        = list(string)
  default     = ["pve1"]
}

variable "uplink_interface" {
  description = "Physical NIC used as uplink for the OVS/Linux bridge."
  type        = string
  default     = "eno1"
}

variable "gateway_host" {
  description = "IP / hostname of the gateway node for NAT and DHCP provisioning."
  type        = string
}

variable "gateway_ssh_user" {
  description = "SSH user on the gateway node."
  type        = string
  default     = "root"
}

variable "gateway_ssh_key" {
  description = "SSH private key content for gateway access."
  type        = string
  sensitive   = true
}

variable "wan_interface" {
  description = "WAN-facing interface on the gateway node."
  type        = string
  default     = "eno2"
}