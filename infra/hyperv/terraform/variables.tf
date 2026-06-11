# TrueNorth Range - Hyper-V Terraform Variables
# Uses the taliesins/hyperv provider to manage VMs on a remote Hyper-V host.

variable "hyperv_host" {
  description = "Hyper-V host name or IP address (WinRM endpoint)"
  type        = string
}

variable "hyperv_user" {
  description = "WinRM username (local or domain, e.g. DOMAIN\\user)"
  type        = string
}

variable "hyperv_password" {
  description = "WinRM password"
  type        = string
  sensitive   = true
}

variable "hyperv_port" {
  description = "WinRM port (5985 for HTTP, 5986 for HTTPS)"
  type        = number
  default     = 5985
}

variable "hyperv_https" {
  description = "Use HTTPS for WinRM transport"
  type        = bool
  default     = false
}

variable "hyperv_insecure" {
  description = "Skip TLS certificate validation"
  type        = bool
  default     = true
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

variable "virtual_switch" {
  description = "Hyper-V virtual switch name (External or Internal)"
  type        = string
  default     = "ExternalSwitch"
}

variable "vhd_path" {
  description = "Path on the Hyper-V host where VM differencing disks are stored"
  type        = string
  default     = "C:\\HyperV\\VMs"
}

variable "golden_image_path" {
  description = "Path to the golden VHDX image to create differencing disks from"
  type        = string
  default     = "C:\\HyperV\\Images\\ubuntu-2204.vhdx"
}

# ── VM Definitions ──────────────────────────────────────────────────────────

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
