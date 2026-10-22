# TrueNorth Range — Linux golden templates (vsphere-iso).
# Build one with:  packer build -only='linux.vsphere-iso.<id>' .  (or ./build.sh <id>)
# The build block is named, so -only needs the build name or a '*.' glob prefix; a bare
# 'vsphere-iso.<id>' matches nothing and Packer exits 0 having built nothing.
# Shared Linux provisioning: pin cloud-init datasources (99-vmware.cfg), SSH host-key unit,
# open-vm-tools + cloud-init (required: require-cloud-init.sh), then cleanup.sh. ISOs are
# on the host-local datastore, so host/datastore are pinned to the ISO host.
#
# Every seed under http/ is a templatefile rendered with local.linux_seed (below): one
# generic build login (ssh_username / ssh_password, hash ssh_password_hash) on every Linux
# template, ADR 0012 decision 5, and the optional build apt proxy (apt_proxy).

locals {
  linux_seed = {
    user          = var.ssh_username
    password_hash = var.ssh_password_hash
    apt_proxy     = var.apt_proxy
  }
}

# ---- ubuntu-lts (24.04 autoinstall via cidata CD) -----------------------------------
source "vsphere-iso" "ubuntu-lts" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "ubuntu-lts"
  notes         = "TrueNorth golden: Ubuntu 24.04 LTS (built by Packer)"
  guest_os_type = "ubuntu64Guest"
  firmware      = "efi"

  CPUs                 = 2
  RAM                  = 2048
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 32768
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_ubuntu}"]
  # Seed and secret CDs (seeds, passwords, hashes) are uploaded to [datastore]
  # packer_cache/; the plugin deletes that ISO on cleanup, this removes the cache too, and
  # remove_cdrom detaches every CD before the VM becomes a template.
  remote_cache_cleanup = true
  remove_cdrom         = true # no clone inherits the install ISO or a seed/secret CD
  cd_content = {
    "user-data" = templatefile("${path.root}/http/ubuntu/user-data.pkrtpl.hcl", local.linux_seed)
    "meta-data" = file("${path.root}/http/ubuntu/meta-data")
  }
  cd_label = "cidata"

  boot_wait = "5s"
  boot_command = [
    "c<wait>",
    "linux /casper/vmlinuz autoinstall ds=nocloud<enter><wait>",
    "initrd /casper/initrd<enter><wait>",
    "boot<enter>"
  ]

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "30m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "ubuntu-lts"
      ovf     = true
      destroy = true
    }
  }
}

# ---- rocky (Rocky Linux 10 kickstart via OEMDRV CD) ---------------------------------
source "vsphere-iso" "rocky" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "rocky"
  notes         = "TrueNorth golden: Rocky Linux 10 (built by Packer)"
  guest_os_type = "other5xLinux64Guest"
  firmware      = "efi"

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

  iso_paths = ["${local.iso_dir}/${var.iso_rocky}"]
  # Seed and secret CDs (seeds, passwords, hashes) are uploaded to [datastore]
  # packer_cache/; the plugin deletes that ISO on cleanup, this removes the cache too, and
  # remove_cdrom detaches every CD before the VM becomes a template.
  remote_cache_cleanup = true
  remove_cdrom         = true # no clone inherits the install ISO or a seed/secret CD
  cd_content = {
    "ks.cfg" = templatefile("${path.root}/http/rocky/ks.cfg.pkrtpl.hcl", local.linux_seed)
  }
  cd_label = "OEMDRV"

  boot_wait = "5s"
  boot_command = [
    "<up><wait>e<wait>",
    "<down><down><end> inst.ks=hd:LABEL=OEMDRV:/ks.cfg",
    "<leftCtrlOn>x<leftCtrlOff>"
  ]

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "30m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "rocky"
      ovf     = true
      destroy = true
    }
  }
}

# ---- kali (preseed over Packer HTTP) ------------------------------------------------
source "vsphere-iso" "kali" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "kali"
  notes         = "TrueNorth golden: Kali Linux (built by Packer)"
  guest_os_type = "debian12_64Guest"
  firmware      = "efi"

  CPUs                 = 2
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_kali}"]
  # Seed and secret CDs (seeds, passwords, hashes) are uploaded to [datastore]
  # packer_cache/; the plugin deletes that ISO on cleanup, this removes the cache too, and
  # remove_cdrom detaches every CD before the VM becomes a template.
  remote_cache_cleanup = true
  remove_cdrom         = true # no clone inherits the install ISO or a seed/secret CD
  http_content = {
    "/kali/preseed.cfg" = templatefile("${path.root}/http/kali/preseed.cfg.pkrtpl.hcl", local.linux_seed)
  }
  http_bind_address = var.http_bind_address
  http_port_min     = var.http_port_min
  http_port_max     = var.http_port_max

  boot_wait = "10s"
  # EFI boots GRUB, not isolinux: type the kernel line on GRUB's command line (as
  # ubuntu-lts does). The isolinux "<esc> auto url=..." form selected a menu entry here and
  # left the installer at an interactive prompt (2026-10-20, ESXi 8.0.3). no_default_route
  # and get_nameservers (the build DHCP gives no DNS) are on the kernel line because netcfg
  # runs before the preseed file is fetched.
  # net.ifnames=0: installed Kali names the NIC eth0; without it netcfg wrote its DHCP config
  # for ens33 and the installed system came up with no network (2026-10-20).
  boot_command = [
    "c<wait>",
    "linux /install.amd/vmlinuz auto=true priority=critical netcfg/no_default_route=true netcfg/get_nameservers=127.0.0.1 url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/kali/preseed.cfg locale=en_US.UTF-8 keymap=us hostname=kali domain=range.local net.ifnames=0 biosdevname=0 --- quiet<enter><wait>",
    "initrd /install.amd/initrd.gz<enter><wait>",
    "boot<enter>"
  ]

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "40m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "kali"
      ovf     = true
      destroy = true
    }
  }
}

# ---- debian13 (preseed over Packer HTTP) --------------------------------------------
source "vsphere-iso" "debian13" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "debian13"
  notes         = "TrueNorth golden: Debian 13 (built by Packer)"
  guest_os_type = "debian12_64Guest"
  firmware      = "efi"

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

  iso_paths = ["${local.iso_dir}/${var.iso_debian}"]
  # Seed and secret CDs (seeds, passwords, hashes) are uploaded to [datastore]
  # packer_cache/; the plugin deletes that ISO on cleanup, this removes the cache too, and
  # remove_cdrom detaches every CD before the VM becomes a template.
  remote_cache_cleanup = true
  remove_cdrom         = true # no clone inherits the install ISO or a seed/secret CD
  http_content = {
    "/debian/preseed.cfg" = templatefile("${path.root}/http/debian/preseed.cfg.pkrtpl.hcl", local.linux_seed)
  }
  http_bind_address = var.http_bind_address
  http_port_min     = var.http_port_min
  http_port_max     = var.http_port_max

  boot_wait = "5s"
  # EFI boots GRUB, not isolinux: type the kernel line on GRUB's command line (as
  # ubuntu-lts does). The isolinux "<esc> auto url=..." form selected a menu entry here and
  # left the installer at an interactive prompt (2026-10-20, ESXi 8.0.3). no_default_route
  # and get_nameservers (the build DHCP gives no DNS) are on the kernel line because netcfg
  # runs before the preseed file is fetched.
  boot_command = [
    "c<wait>",
    "linux /install.amd/vmlinuz auto=true priority=critical netcfg/no_default_route=true netcfg/get_nameservers=127.0.0.1 url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/debian/preseed.cfg locale=en_US.UTF-8 keymap=us hostname=debian13 domain=range.local --- quiet<enter><wait>",
    "initrd /install.amd/initrd.gz<enter><wait>",
    "boot<enter>"
  ]

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "30m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "debian13"
      ovf     = true
      destroy = true
    }
  }
}

# ---- parrot (preseed over Packer HTTP; FRAGILE — see README/preseed note) -----------
source "vsphere-iso" "parrot" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "parrot"
  notes         = "TrueNorth golden: Parrot Security 7.3 (built by Packer)"
  guest_os_type = "debian12_64Guest"
  firmware      = "efi"

  CPUs                 = 2
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_parrot}"]
  # Seed and secret CDs (seeds, passwords, hashes) are uploaded to [datastore]
  # packer_cache/; the plugin deletes that ISO on cleanup, this removes the cache too, and
  # remove_cdrom detaches every CD before the VM becomes a template.
  remote_cache_cleanup = true
  remove_cdrom         = true # no clone inherits the install ISO or a seed/secret CD
  http_content = {
    "/parrot/preseed.cfg" = templatefile("${path.root}/http/parrot/preseed.cfg.pkrtpl.hcl", local.linux_seed)
  }
  http_bind_address = var.http_bind_address
  http_port_min     = var.http_port_min
  http_port_max     = var.http_port_max

  boot_wait = "10s"
  boot_command = [
    "<esc><wait>",
    "auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/parrot/preseed.cfg ",
    "locale=en_US keymap=us hostname=parrot domain=range.local ",
    "<enter>"
  ]

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "40m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "parrot"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Shared Linux build (open-vm-tools, cloud-init datasource pin, cleanup) ----------
build {
  name = "linux"
  sources = [
    "source.vsphere-iso.ubuntu-lts",
    "source.vsphere-iso.rocky",
    "source.vsphere-iso.kali",
    "source.vsphere-iso.debian13",
    "source.vsphere-iso.parrot",
  ]

  provisioner "file" {
    source      = "${path.root}/files/linux/99-vmware.cfg"
    destination = "/tmp/99-vmware.cfg"
  }

  # Build apt/dnf proxy (apt_proxy -> TN-DEPOT01 apt-cacher-ng); a no-op when empty.
  # The guests keep their stock http:// sources; the proxy is a forward cache, not a mirror.
  provisioner "shell" {
    environment_vars = ["APT_PROXY=${var.apt_proxy}"]
    script           = "${path.root}/files/linux/apt-proxy.sh"
  }

  # cleanup.sh deletes the SSH host keys; this unit regenerates any that are missing before
  # sshd starts on a clone (cloud-init's ssh module normally does it first).
  provisioner "file" {
    source      = "${path.root}/files/linux/tn-ssh-hostkeys.service"
    destination = "/tmp/tn-ssh-hostkeys.service"
  }
  provisioner "shell" {
    inline = [
      "sudo install -m 0644 /tmp/tn-ssh-hostkeys.service /etc/systemd/system/tn-ssh-hostkeys.service",
      "sudo systemctl daemon-reload",
      "sudo systemctl enable tn-ssh-hostkeys.service",
    ]
  }

  # Debian/Ubuntu family: ensure tools + cloud-init, pin datasource.
  provisioner "shell" {
    except = ["vsphere-iso.rocky"]
    inline = [
      "sudo install -D -m 0644 /tmp/99-vmware.cfg /etc/cloud/cloud.cfg.d/99-vmware.cfg",
      "export DEBIAN_FRONTEND=noninteractive",
      "sudo apt-get update || true",
      "sudo apt-get install -y open-vm-tools cloud-init curl wget git net-tools || echo 'apt baseline skipped (no reachable mirror)'",
      "sudo systemctl enable open-vm-tools || true",
    ]
  }

  # cloud-init is NOT optional: the worker configures Linux range VMs only through its
  # VMware datasource (no vSphere Linux customization). From the mirror/apt proxy, else the
  # install ISO's pool; neither: the build fails (no template).
  provisioner "shell" {
    except = ["vsphere-iso.rocky"]
    script = "${path.root}/files/linux/install-cloud-init.sh"
  }

  # Rocky: dnf equivalents.
  provisioner "shell" {
    only = ["vsphere-iso.rocky"]
    inline = [
      "sudo install -m 0644 /tmp/99-vmware.cfg /etc/cloud/cloud.cfg.d/99-vmware.cfg",
      "sudo dnf install -y open-vm-tools cloud-init curl wget git net-tools || echo 'dnf baseline skipped (no reachable mirror)'",
      "sudo systemctl enable vmtoolsd || true",
    ]
  }

  # Kali: full metapackage is huge and online-only — best effort, never fail the build.
  provisioner "shell" {
    only = ["vsphere-iso.kali"]
    inline = [
      "export DEBIAN_FRONTEND=noninteractive",
      "sudo -E apt-get install -y kali-linux-default || echo 'kali-linux-default skipped (no reachable mirror); install at deploy from depot'",
    ]
  }

  # Refuse a template the worker cannot configure: cloud-init + VMware datasource, Tools,
  # host-key regeneration. Fails the build otherwise.
  provisioner "shell" {
    script = "${path.root}/files/linux/require-cloud-init.sh"
  }

  provisioner "shell" {
    script = "${path.root}/files/linux/cleanup.sh"
  }
}
