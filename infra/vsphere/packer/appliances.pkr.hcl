# TrueNorth Range — appliance golden templates (vsphere-iso).
# These are KEYSTROKE builds: no unattended answer-file path exists for the installers,
# so boot_command drives the UI blind. They are the least reliable builds in the set and
# WILL need on-site timing tuning. All three use communicator="none"; after boot_command
# Packer powers the VM off through vCenter and converts it to a template. Read the README
# "Appliance builds" section before running, and expect to finish pfSense/SO by hand the
# first time, then capture the VM as the template.
#
# ISOs for all three are NOT on the lab datastore yet (iso_* default "MUST-UPLOAD-FIRST").

# ---- pfsense (pfSense CE 2.7.2; 2 vmxnet3 NICs) -------------------------------------
source "vsphere-iso" "pfsense" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "pfsense"
  notes         = "TrueNorth golden: pfSense CE 2.7.2 (keystroke build)"
  guest_os_type = "freebsd13_64Guest"
  firmware      = "bios"

  CPUs                 = 2
  RAM                  = 2048
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 20480
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }
  network_adapters {
    network      = local.pfsense_lan
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_pfsense}"]

  communicator = "none"
  boot_wait    = "40s"
  boot_command = [
    "<enter><wait5>",           # accept copyright/license
    "<enter><wait5>",           # Install pfSense
    "<enter><wait5>",           # keymap default
    "<enter><wait5>",           # Auto (ZFS)
    "<enter><wait5>",           # Proceed
    "<enter><wait5>",           # stripe - no redundancy
    "<spacebar><enter><wait5>", # select disk
    "<left><enter><wait180>",   # confirm -> install
    "<enter><wait30>"           # reboot
  ]
  shutdown_timeout = "15m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "pfsense"
      ovf     = true
      destroy = true
    }
  }
}

# ---- securityonion (SO 2.4, Oracle Linux 9 based; base OS only, so-setup at deploy) --
source "vsphere-iso" "securityonion" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "securityonion"
  notes         = "TrueNorth golden: Security Onion 2.4 base OS (so-setup deferred to deploy)"
  guest_os_type = "other5xLinux64Guest"
  firmware      = "efi"

  CPUs                 = 4
  RAM                  = 16384
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 204800
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network # mgmt
    network_card = "vmxnet3"
  }
  network_adapters {
    network      = local.pfsense_lan # sniff/monitor
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_securityonion}"]

  communicator = "none"
  boot_wait    = "60s"
  # SO installer: pick the graphical/standard install entry, then its OL9 Anaconda runs.
  # Left minimal on purpose — the SO ISO installs the base OS, and so-setup is run at
  # deploy time. Finish the OS install interactively on first build, then template it.
  boot_command     = ["<enter>"]
  shutdown_timeout = "30m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "securityonion"
      ovf     = true
      destroy = true
    }
  }
}

# ---- vyos (VyOS 1.4; open-vm-tools built in; 2 NICs) --------------------------------
source "vsphere-iso" "vyos" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "vyos"
  notes         = "TrueNorth golden: VyOS 1.4 (keystroke build; not yet in catalogue)"
  guest_os_type = "debian12_64Guest"
  firmware      = "bios"

  CPUs                 = 1
  RAM                  = 2048
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 8192
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }
  network_adapters {
    network      = local.pfsense_lan
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_vyos}"]

  communicator = "none"
  boot_wait    = "40s"
  # Live boot auto-logs in as vyos/vyos on the console, then `install image` with defaults.
  boot_command = [
    "vyos<enter><wait>",   # login user
    "vyos<enter><wait10>", # login password
    "install image<enter><wait5>",
    "<enter><wait5>",    # Would you like to continue? Yes
    "<enter><wait5>",    # partition: Auto
    "<enter><wait5>",    # select drive
    "<enter><wait5>",    # root partition size default
    "<enter><wait30>",   # confirm
    "vyos<enter><wait>", # set admin password
    "vyos<enter><wait5>",
    "<enter><wait5>", # grub target default
    "<wait30>"
  ]
  shutdown_timeout = "15m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "vyos"
      ovf     = true
      destroy = true
    }
  }
}

build {
  name = "appliances"
  sources = [
    "source.vsphere-iso.pfsense",
    "source.vsphere-iso.securityonion",
    "source.vsphere-iso.vyos",
  ]
  # communicator="none": nothing to provision in-guest. pfSense config.xml
  # (http/pfsense/config.xml) and SO so-setup are applied at deploy time.
}
