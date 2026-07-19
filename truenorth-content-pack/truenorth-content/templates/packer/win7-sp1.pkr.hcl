# Golden image win7-sp1 — vSphere-iso build -> vCenter Content Library.
# Catalogue: os=windows 7 SP1 (legacy victim); base_iso=licensed media (no eval); sensor_baked=yes.
# NOTE: Default legacy/vulnerable host. No public eval ISO - needs entitled media.
# Do NOT run `packer build` here — files for human review + Content Library upload.
variable "vsphere_server"   { type = string }   # vCenter FQDN
variable "vsphere_user"     { type = string }
variable "vsphere_password" { type = string }   # sensitive
variable "vsphere_datacenter"  { type = string }
variable "vsphere_cluster"     { type = string }
variable "vsphere_datastore"   { type = string }
variable "vsphere_content_library" { type = string }   # build target — golden images land here
variable "vsphere_network"     { type = string }       # build VLAN (isolated, no egress)

source "vsphere-iso" "win7-sp1" {
  vcenter_server      = var.vsphere_server
  username            = var.vsphere_user
  password            = var.vsphere_password
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  datastore           = var.vsphere_datastore
  content_library     = var.vsphere_content_library
  host                = ""    # cluster-driven
  network             = var.vsphere_network
  guest_os_type       = "windows9_64Guest"

  iso_paths = ["[${var.vsphere_datastore}] licensed media (no eval)"]
  iso_checksum = "none"   # operator pins the real checksum per Content Library import

  vm_name      = "win7-sp1"
  cpus         = 2
  memory       = 4096
  disk_size    = 25600
  disk_thin_provisioned = true

  boot_command     = []   # operator fills per-OS unattend/cloud-init; kept empty for review
  shutdown_command = ""    # operator-provided per OS
  # sensor_baked=yes: provision the SecurityOnion sensor agent / collector in build (per notes).
}

build {
  sources = ["source.vsphere-iso.win7-sp1"]
  # Provisioners: install + configure the baked sensor/collector when sensor_baked=yes.
  # Left minimal for review; operator finalizes per OS (vSphere guest tools, OpenSearch shipper, etc.).
}
