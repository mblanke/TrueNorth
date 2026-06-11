# TrueNorth Range — Packer template for Ubuntu 22.04 on vSphere
# Builds a cloud-init ready template via the vsphere-iso builder.
packer {
  required_plugins {
    vsphere = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/vsphere"
    }
  }
}

source "vsphere-iso" "ubuntu-2204" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection

  datacenter = var.vsphere_datacenter
  cluster    = var.vsphere_cluster
  datastore  = var.vsphere_datastore
  folder     = var.vsphere_folder

  vm_name              = "ubuntu-2204-cloud"
  notes                = "TrueNorth Ubuntu 22.04 template built by Packer"

  guest_os_type = "ubuntu64Guest"
  firmware      = "efi"

  CPUs            = 2
  RAM             = 2048
  RAM_reserve_all = false

  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 32768
    disk_thin_provisioned = true
  }

  network_adapters {
    network      = var.vsphere_network
    network_card = "vmxnet3"
  }

  iso_url      = "https://releases.ubuntu.com/22.04/ubuntu-22.04.3-live-server-amd64.iso"
  iso_checksum = "sha256:a4acfda10b18da50e2ec50ccaf860d7f20b389df8765611142305c0e911d16fd"

  http_directory = "../proxmox/packer/http"

  boot_wait = "5s"
  boot_command = [
    "<esc><esc><esc><esc>e<wait>",
    "<down><down><down><end>",
    " autoinstall ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/",
    "<f10>"
  ]

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "25m"

  # Publish to Content Library if configured
  dynamic "content_library_destination" {
    for_each = var.content_library != "" ? [1] : []
    content {
      library = var.content_library
      name    = "ubuntu-2204-cloud"
      ovf     = true
      destroy = false
    }
  }

  # Convert to template after build
  convert_to_template = var.content_library == "" ? true : false

  export {
    force = true
  }
}

build {
  sources = ["source.vsphere-iso.ubuntu-2204"]

  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      # Install VMware Tools (open-vm-tools) instead of QEMU guest agent
      "sudo apt-get install -y open-vm-tools cloud-init curl wget git",
      "sudo apt-get install -y auditd sysstat net-tools",
      "sudo systemctl enable open-vm-tools",
      "sudo cloud-init clean",
      "sudo truncate -s 0 /etc/machine-id",
    ]
  }

  provisioner "shell" {
    inline = [
      "wget -q https://packages.microsoft.com/config/ubuntu/22.04/packages-microsoft-prod.deb -O /tmp/packages-microsoft-prod.deb",
      "sudo dpkg -i /tmp/packages-microsoft-prod.deb || true",
      "sudo apt-get update",
      "sudo apt-get install -y sysinternalsebpf sysmonforlinux || echo 'Sysmon install skipped'",
    ]
  }
}
