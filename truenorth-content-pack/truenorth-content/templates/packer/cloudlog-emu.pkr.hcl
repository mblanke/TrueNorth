# Golden image cloudlog-emu — DERIVED (no ISO). Built by cloning + provisioning a base template.
# Catalogue: base_iso=derived from ubuntu-lts; sensor_baked=n/a; packer_status=todo(vsphere).
# NOTE: ALJQ 009.03 cloud analysis EO. Serves emulated cloud log sets.
# Do NOT run `packer build` here — files for human review. Derived images use vsphere-clone from a
# base golden template (below), NOT vsphere-iso.
variable "vsphere_server"   { type = string }   # vCenter FQDN
variable "vsphere_user"     { type = string }
variable "vsphere_password" { type = string }   # sensitive
variable "vsphere_datacenter"  { type = string }
variable "vsphere_cluster"     { type = string }
variable "vsphere_datastore"   { type = string }
variable "vsphere_content_library" { type = string }   # build target — golden images land here
variable "vsphere_network"     { type = string }       # build VLAN (isolated, no egress)

variable "base_template_cloudlog-emu" { type = string, default = "ubuntu-lts" }   # base golden template to clone from

source "vsphere-clone" "cloudlog-emu" {
  vcenter_server      = var.vsphere_server
  username            = var.vsphere_user
  password            = var.vsphere_password
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  datastore           = var.vsphere_datastore
  content_library     = var.vsphere_content_library
  network             = var.vsphere_network
  template            = var.base_template_cloudlog-emu
  vm_name             = "cloudlog-emu"
  linked_clone        = false   # full snapshot of the base, then provision on top
}
build {
  sources = ["source.vsphere-clone.cloudlog-emu"]
  # Provisioners per notes: REMnux/SIFT installer, service emulators, usersim noise-floor agent,
  # precomp-host staged artifacts, blueteam-soc composite stack, etc.
}
