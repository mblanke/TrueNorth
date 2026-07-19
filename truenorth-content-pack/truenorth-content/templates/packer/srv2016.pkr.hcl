# Golden image srv2016 — vSphere-iso build -> vCenter Content Library.
# Catalogue: os=windows Server 2016 (legacy DC/file); base_iso=SERVER_2016_EVAL_x64FRE_en-us.iso; sensor_baked=yes.
# NOTE: Legacy AD/file. Distinct eval ISO from 2019/2022.
# Do NOT run `packer build` here — files for human review + Content Library upload.
variable "vsphere_server"   { type = string }   # vCenter FQDN
variable "vsphere_user"     { type = string }
variable "vsphere_password" { type = string }   # sensitive
variable "vsphere_datacenter"  { type = string }
variable "vsphere_cluster"     { type = string }
variable "vsphere_datastore"   { type = string }
variable "vsphere_content_library" { type = string }   # build target — golden images land here
variable "vsphere_network"     { type = string }       # build VLAN (isolated, no egress)

source "vsphere-iso" "srv2016" {
  vcenter_server      = var.vsphere_server
  username            = var.vsphere_user
  password            = var.vsphere_password
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  datastore           = var.vsphere_datastore
  content_library     = var.vsphere_content_library
  host                = ""    # cluster-driven
  network             = var.vsphere_network
  guest_os_type       = "windows9Server"

  iso_paths = ["[${var.vsphere_datastore}] SERVER_2016_EVAL_x64FRE_en-us.iso"]
  iso_checksum = "none"   # operator pins the real checksum per Content Library import

  vm_name      = "srv2016"
  cpus         = 2
  memory       = 4096
  disk_size    = 30720
  disk_thin_provisioned = true

  boot_command     = []   # operator fills per-OS unattend/cloud-init; kept empty for review
  shutdown_command = ""    # operator-provided per OS
  # sensor_baked=yes: provision the SecurityOnion sensor agent / collector in build (per notes).
}

build {
  sources = ["source.vsphere-iso.srv2016"]
  # Provisioners: install + configure the baked sensor/collector when sensor_baked=yes.
  # Left minimal for review; operator finalizes per OS (vSphere guest tools, OpenSearch shipper, etc.).
}
