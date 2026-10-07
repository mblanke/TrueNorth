# TrueNorth Range — Linux golden templates (vsphere-iso).
# Build one with:  packer build -only='linux.vsphere-iso.<id>' .  (or ./build.sh <id>)
# The build block is named, so -only needs the build name or a '*.' glob prefix; a bare
# 'vsphere-iso.<id>' matches nothing and Packer exits 0 having built nothing.
# Shared Linux provisioning: pin cloud-init datasources (99-vmware.cfg), ensure
# open-vm-tools + cloud-init, then cleanup.sh. ISOs are pre-staged on the host-local
# datastore, so host/datastore are pinned to the ISO host.

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
  cd_files  = ["${path.root}/http/ubuntu/user-data", "${path.root}/http/ubuntu/meta-data"]
  cd_label  = "cidata"

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
  cd_files  = ["${path.root}/http/rocky/ks.cfg"]
  cd_label  = "OEMDRV"

  boot_wait = "5s"
  boot_command = [
    "<up><wait>e<wait>",
    "<down><down><end> inst.ks=hd:LABEL=OEMDRV:/ks.cfg",
    "<leftCtrlOn>x<leftCtrlOff>"
  ]

  ssh_username = "rocky"
  ssh_password = "rocky"
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

  iso_paths         = ["${local.iso_dir}/${var.iso_kali}"]
  http_directory    = "${path.root}/http"
  http_bind_address = var.http_bind_address
  http_port_min     = var.http_port_min
  http_port_max     = var.http_port_max

  boot_wait = "10s"
  boot_command = [
    "<esc><wait>",
    "auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/kali/preseed.cfg ",
    "locale=en_US keymap=us hostname=kali domain=range.local ",
    "<enter>"
  ]

  ssh_username = "kali"
  ssh_password = "kali"
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

  iso_paths         = ["${local.iso_dir}/${var.iso_debian}"]
  http_directory    = "${path.root}/http"
  http_bind_address = var.http_bind_address
  http_port_min     = var.http_port_min
  http_port_max     = var.http_port_max

  boot_wait = "5s"
  boot_command = [
    "<esc><wait>",
    "auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/debian/preseed.cfg ",
    "locale=en_US keymap=us hostname=debian13 domain=range.local ",
    "<enter>"
  ]

  ssh_username = "debian"
  ssh_password = "debian"
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

  iso_paths         = ["${local.iso_dir}/${var.iso_parrot}"]
  http_directory    = "${path.root}/http"
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

  ssh_username = "parrot"
  ssh_password = "parrot"
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

  # Debian/Ubuntu family: ensure tools + cloud-init, pin datasource, optional depot mirror.
  provisioner "shell" {
    except           = ["vsphere-iso.rocky"]
    environment_vars = ["DEPOT_URL=${var.depot_url}"]
    inline = [
      "sudo install -m 0644 /tmp/99-vmware.cfg /etc/cloud/cloud.cfg.d/99-vmware.cfg",
      "export DEBIAN_FRONTEND=noninteractive",
      "if [ -n \"$DEPOT_URL\" ]; then echo \"deb $DEPOT_URL/apt $(. /etc/os-release; echo $VERSION_CODENAME) main\" | sudo tee /etc/apt/sources.list.d/truenorth-depot.list; fi",
      "sudo apt-get update || true",
      "sudo apt-get install -y open-vm-tools cloud-init curl wget git net-tools || echo 'apt baseline skipped (no reachable mirror)'",
      "sudo systemctl enable open-vm-tools || true",
    ]
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
      "sudo apt-get install -y kali-linux-default || echo 'kali-linux-default skipped (no reachable mirror); install at deploy from depot'",
    ]
  }

  provisioner "shell" {
    script = "${path.root}/files/linux/cleanup.sh"
  }
}
