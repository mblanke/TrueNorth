# TrueNorth Range — appliance golden templates (vsphere-iso).
# These are KEYSTROKE builds: no unattended answer-file path exists for the installers,
# so boot_command drives the UI blind. They are the least reliable builds in the set and
# WILL need on-site timing tuning. All three use communicator="none", and with no
# communicator Packer does NOT power the VM off: shutdown_command is unused and Packer only
# waits shutdown_timeout for the GUEST to power itself off, then converts it to a template.
# So each boot_command must end by powering the guest off, or the build fails "timeout
# while waiting for machine to shutdown" (pfSense, 2026-10-21).
#
# pfSense and VyOS finish themselves from a second CD and power off ONLY when every
# finishing step worked: a failed finish leaves the VM running, the build fails at
# shutdown_timeout and no template is made. So a green pfSense/VyOS build is a usable
# template (VMware Tools, boot hook / cloud-init datasource); build-phase.sh then proves it
# with a test clone in "tools" mode before registering. Security Onion is still finished by
# hand. Read the README "Appliance builds" section before running.
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
  # Second CD (label TNPFSENSE): everything the template finish needs, with no network.
  # tn-pfsense-finish.sh (run from the installer's Shell) pkg-adds the Open-VM-Tools set in
  # pkgs.lock (sha256-checked; the .pkg files are staged into stage/pfsense/ by
  # scripts/lab/stage_pfsense_pkgs.py, which build-phase.sh checks first), installs
  # tn-pfsense-config + tn-vmtools.sh and writes the template's config.xml (WAN vmx0,
  # LAN vmx1, SSH, boot hook, admin password = ssh_password_hash: ADR 0012 decision 5).
  cd_files = [
    "${path.root}/files/pfsense/tn-pfsense-finish.sh",
    "${path.root}/files/pfsense/tn-pfsense-config",
    "${path.root}/files/pfsense/tn-vmtools.sh",
    "${path.root}/files/pfsense/pkgs.lock",
    "${path.root}/stage/pfsense/*.pkg",
  ]
  cd_content = {
    "tn-admin.hash" = var.ssh_password_hash
  }
  cd_label = "TNPFSENSE"

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
    "<left><enter><wait240>",   # confirm -> install (ZFS extract; the next dialog just waits)
    # Final "Complete" dialog [Reboot] [Shell], focus on Reboot. Do NOT reboot: nothing
    # would ever power the VM off (2026-10-21: 15m shutdown timeout after "Reboot"). <tab>
    # moves to Shell (and, if a "Manual Configuration" Yes/No-default-No dialog comes first,
    # wraps to Yes = a chroot shell in the new system); either way a root shell.
    "<tab><enter><wait10>",
    # Mount the second CD and finish the template; the script powers off only on success.
    "mkdir -p /tmp/tncd; mount_cd9660 /dev/iso9660/TNPFSENSE /tmp/tncd || mount_cd9660 /dev/cd1 /tmp/tncd; sh /tmp/tncd/tn-pfsense-finish.sh<enter>"
  ]
  # The finish (a few pkg adds) powers off within a minute or two; 8m is margin.
  shutdown_timeout = "8m"

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
  # Second CD (label TNVYOS). tn-vyos-prep.sh drops the live config's NIC MAC pins (hw-id)
  # and saves it; tn-vyos-install.py drives `install image` by matching each prompt's text
  # (not keystroke timing: the rolling installer's questions change between builds), copies
  # that config, sets the vyos password (ssh_password: ADR 0012 decision 5), gives the image
  # a cloud-init datasource list starting with VMware (worker/vyos_config.py's guestinfo
  # user data) and powers off only on success. Open-VM-Tools is part of the VyOS image.
  cd_files = [
    "${path.root}/files/vyos/tn-vyos-prep.sh",
    "${path.root}/files/vyos/tn-vyos-install.py",
  ]
  cd_content = {
    "tn-vyos.pw" = var.ssh_password
  }
  cd_label = "TNVYOS"

  communicator = "none"
  # GRUB's menu times out to the live entry; the console login prompt comes up after it.
  boot_wait = "90s"
  boot_command = [
    "vyos<enter><wait5>",  # live login user
    "vyos<enter><wait15>", # live login password
    "sudo mkdir -p /run/tncd && sudo mount -o ro /dev/disk/by-label/TNVYOS /run/tncd<enter><wait5>",
    "/bin/vbash /run/tncd/tn-vyos-prep.sh<enter><wait30>",
    "sudo python3 /run/tncd/tn-vyos-install.py<enter>"
  ]
  # install image copies the squashfs (a few minutes); the script powers off when done.
  shutdown_timeout = "20m"

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
  # communicator="none": nothing to provision in-guest; pfSense and VyOS finish from their
  # second CD (above). Each range's pfSense config.xml is rendered by the worker
  # (worker/pfsense_config.py) and put in the VM's guestinfo; the template's
  # tn-pfsense-config applies it at boot (VMware Tools: tn-vmtools.sh). VyOS gets its
  # commands as cloud-init user data (worker/vyos_config.py). SO so-setup runs at deploy.
}
