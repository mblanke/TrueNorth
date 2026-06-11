# TrueNorth Range - vSphere Packer Variables
# Shared variable file for all vsphere-iso builders.
# Override values in truenorth.auto.pkrvars.hcl (git-ignored).

variable "vcenter_server" {
  description = "vCenter FQDN or IP"
  type        = string
  default     = "vcenter.lab.local"
}

variable "vcenter_username" {
  description = "vCenter SSO user"
  type        = string
  default     = "administrator@vsphere.local"
}

variable "vcenter_password" {
  description = "vCenter SSO password"
  type        = string
  sensitive   = true
  default     = ""
}

variable "vsphere_datacenter" {
  description = "Target vSphere datacenter"
  type        = string
  default     = "Datacenter"
}

variable "vsphere_cluster" {
  description = "Target vSphere compute cluster"
  type        = string
  default     = "Cluster"
}

variable "vsphere_datastore" {
  description = "Datastore for the built template"
  type        = string
  default     = "datastore1"
}

variable "vsphere_network" {
  description = "Portgroup / distributed portgroup for the builder NIC"
  type        = string
  default     = "VM Network"
}

variable "vsphere_folder" {
  description = "VM folder for finished templates"
  type        = string
  default     = "truenorth/templates"
}

variable "content_library" {
  description = "Content Library to publish the OVF template to (leave empty to skip)"
  type        = string
  default     = ""
}

variable "insecure_connection" {
  description = "Skip TLS certificate validation (true for lab environments)"
  type        = bool
  default     = true
}

variable "ssh_username" {
  type    = string
  default = "ubuntu"
}

variable "ssh_password" {
  type      = string
  sensitive = true
  default   = "ubuntu"
}
