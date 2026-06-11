# TrueNorth Range - vSphere Terraform Variables
# Manages VM clones via the official HashiCorp vSphere provider.

variable "vsphere_server" {
  description = "vCenter Server FQDN or IP"
  type        = string
}

variable "vsphere_user" {
  description = "vCenter SSO user (e.g. administrator@vsphere.local)"
  type        = string
}

variable "vsphere_password" {
  description = "vCenter SSO password"
  type        = string
  sensitive   = true
}

variable "vsphere_allow_unverified_ssl" {
  description = "Skip TLS certificate validation (set true for lab environments)"
  type        = bool
  default     = false
}

variable "range_id" {
  description = "Unique range identifier used to tag all resources"
  type        = string
}

variable "range_name" {
  description = "Human-readable range name"
  type        = string
  default     = "truenorth-range"
}

# ── Placement ───────────────────────────────────────────────────────────────

variable "datacenter" {
  description = "vSphere Datacenter name"
  type        = string
}

variable "cluster" {
  description = "vSphere Cluster or host name"
  type        = string
}

variable "datastore" {
  description = "Datastore to place VM disks on"
  type        = string
}

variable "network" {
  description = "Default virtual machine network/portgroup"
  type        = string
  default     = "VM Network"
}

variable "resource_pool" {
  description = "Resource pool path (leave empty to use cluster root)"
  type        = string
  default     = ""
}

variable "folder" {
  description = "VM folder path within the datacenter"
  type        = string
  default     = "truenorth"
}

# ── VM Definitions ──────────────────────────────────────────────────────────

variable "template_name" {
  description = "vSphere VM template to clone from (must exist in the datacenter)"
  type        = string
  default     = "ubuntu-2204-cloud"
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
  default = []
}

variable "dns_servers" {
  description = "DNS servers to configure via VMware guest customization"
  type        = list(string)
  default     = ["8.8.8.8", "8.8.4.4"]
}
