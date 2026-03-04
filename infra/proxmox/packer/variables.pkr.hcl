# TrueNorth Range — Shared Packer variables for all templates

variable "proxmox_url" {
  type        = string
  description = "Proxmox API endpoint URL"
  default     = "https://pve:8006/api2/json"
}

variable "proxmox_username" {
  type        = string
  description = "Proxmox API username"
  default     = "root@pam"
}

variable "proxmox_password" {
  type        = string
  description = "Proxmox API password"
  sensitive   = true
}

variable "proxmox_node" {
  type        = string
  description = "Proxmox node to build on"
  default     = "pve"
}

variable "iso_storage_pool" {
  type        = string
  description = "Proxmox storage pool for ISO images"
  default     = "local"
}

variable "vm_storage_pool" {
  type        = string
  description = "Proxmox storage pool for VM disks"
  default     = "local-lvm"
}

variable "vlan_tag" {
  type        = number
  description = "Optional VLAN tag for build network interfaces (-1 to disable)"
  default     = -1
}

variable "ssh_timeout" {
  type        = string
  description = "Timeout for SSH provisioner connections"
  default     = "30m"
}

variable "winrm_timeout" {
  type        = string
  description = "Timeout for WinRM provisioner connections"
  default     = "60m"
}