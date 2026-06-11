# TrueNorth Range - Hyper-V Packer Variables
# NOTE: hyperv-iso builds MUST run on a Windows host with the Hyper-V role installed.
# Run from an elevated PowerShell session: packer build -only=hyperv-iso.ubuntu-2204 .

variable "hyperv_switch" {
  description = "Hyper-V virtual switch to connect the build VM to (must be External)"
  type        = string
  default     = "ExternalSwitch"
}

variable "hyperv_generation" {
  description = "Hyper-V VM generation (1 or 2). Gen 2 requires EFI-booting ISOs."
  type        = number
  default     = 2
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

variable "winrm_username" {
  type    = string
  default = "Administrator"
}

variable "winrm_password" {
  type      = string
  sensitive = true
  default   = "Packer!"
}

variable "output_base_dir" {
  description = "Directory on the build host where finished VHDX files are stored"
  type        = string
  default     = "C:\\HyperV\\Images"
}
