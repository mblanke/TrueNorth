# TrueNorth Range - Proxmox Terraform Variables
# Designed for multi-node Proxmox cluster supporting 70k VMs

variable "pm_api_url" {
  description = "Proxmox API URL"
  type        = string
}

variable "pm_user" {
  description = "Proxmox API user"
  type        = string
  default     = "root@pam"
}

variable "pm_password" {
  description = "Proxmox API password"
  type        = string
  sensitive   = true
}

# Alternatively use API token (preferred for production)
variable "pm_api_token_id" {
  description = "Proxmox API Token ID"
  type        = string
  default     = ""
}

variable "pm_api_token_secret" {
  description = "Proxmox API Token Secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "range_id" {
  description = "Unique range identifier"
  type        = string
}

variable "range_name" {
  description = "Human-readable range name"
  type        = string
  default     = "truenorth-range"
}

variable "template_name" {
  description = "Proxmox VM template to clone from"
  type        = string
  default     = "ubuntu-2204-cloud"
}

# Multi-node support for load distribution
variable "target_nodes" {
  description = "List of Proxmox nodes available for VM placement"
  type        = list(string)
  default     = ["pve"]
}

variable "placement_strategy" {
  description = "VM placement strategy: round-robin, fill-first, or resource-aware"
  type        = string
  default     = "round-robin"
  validation {
    condition     = contains(["round-robin", "fill-first", "resource-aware"], var.placement_strategy)
    error_message = "placement_strategy must be round-robin, fill-first, or resource-aware"
  }
}

variable "vm_definitions" {
  description = "List of VMs to create from the range template"
  type = list(object({
    name     = string
    role     = string
    cores    = number
    memory   = number
    disk_gb  = number
    ip       = string
    gateway  = string
    vlan_tag = number
  }))
  default = [
    {
      name     = "jump-box"
      role     = "jump"
      cores    = 2
      memory   = 2048
      disk_gb  = 32
      ip       = "10.0.1.10/24"
      gateway  = "10.0.1.1"
      vlan_tag = 100
    },
    {
      name     = "dc-01"
      role     = "dc"
      cores    = 4
      memory   = 4096
      disk_gb  = 64
      ip       = "10.0.2.10/24"
      gateway  = "10.0.2.1"
      vlan_tag = 200
    },
    {
      name     = "ws-01"
      role     = "workstation"
      cores    = 2
      memory   = 2048
      disk_gb  = 32
      ip       = "10.0.3.10/24"
      gateway  = "10.0.3.1"
      vlan_tag = 300
    },
  ]
}

# Network isolation settings
variable "vlan_base" {
  description = "Base VLAN ID for range isolation (each range gets vlan_base + offset)"
  type        = number
  default     = 100
}

variable "storage_pool" {
  description = "Proxmox storage pool for VM disks"
  type        = string
  default     = "local-lvm"
}

variable "cloud_init_user" {
  description = "Cloud-init default user"
  type        = string
  default     = "truenorth"
}

variable "cloud_init_ssh_keys" {
  description = "SSH public keys for cloud-init"
  type        = string
  default     = ""
}