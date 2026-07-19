# Golden image ubuntu-lts — vSphere-iso build -> vCenter Content Library.
# Catalogue: os=linux 24.04 (web/app/db victim); base_iso=ubuntu-24.04-live-server-amd64.iso; sensor_baked=yes.
# NOTE: DISCREPANCY: catalogue=24.04 but packer files ship 22.04.3. Pick one before build.
# Do NOT run `packer build` here — files for human review + Content Library upload.
variable "vsphere_server"   { type = string }   # vCenter FQDN
variable "vsphere_user"     { type = string }
variable "vsphere_password" { type = string }   # sensitive
variable "vsphere_datacenter"  { type = string }
variable "vsphere_cluster"     { type = string }
variable "vsphere_datastore"   { type = string }
variable "vsphere_content_library" { type = string }   # build target — golden images land here
variable "vsphere_network"     { type = string }       # build VLAN (isolated, no egress)

source "vsphere-iso" "ubuntu-lts" {
  vcenter_server      = var.vsphere_server
  username            = var.vsphere_user
  password            = var.vsphere_password
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  datastore           = var.vsphere_datastore
  content_library     = var.vsphere_content_library
  host                = ""    # cluster-driven
  network             = var.vsphere_network
  guest_os_type       = "ubuntu64Guest"

  iso_paths = ["[${var.vsphere_datastore}] ubuntu-24.04-live-server-amd64.iso"]
  iso_checksum = "none"   # operator pins the real checksum per Content Library import

  vm_name      = "ubuntu-lts"
  cpus         = 2
  memory       = 4096
  disk_size    = 15360
  disk_thin_provisioned = true

  boot_command     = []   # operator fills per-OS unattend/cloud-init; kept empty for review
  shutdown_command = ""    # operator-provided per OS
  # sensor_baked=yes: provision the SecurityOnion sensor agent / collector in build (per notes).
}

build {
  sources = ["source.vsphere-iso.ubuntu-lts"]
  # Provisioners: install + configure the baked sensor/collector when sensor_baked=yes.
  # Left minimal for review; operator finalizes per OS (vSphere guest tools, OpenSearch shipper, etc.).
}
