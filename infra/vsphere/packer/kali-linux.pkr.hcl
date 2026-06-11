# TrueNorth Range — Packer template for Kali Linux on vSphere
packer {
  required_plugins {
    vsphere = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/vsphere"
    }
  }
}

variable "kali_iso_url" {
  type    = string
  default = "https://cdimage.kali.org/kali-2024.1/kali-linux-2024.1-installer-amd64.iso"
}

variable "kali_iso_checksum" {
  type    = string
  default = "sha256:a1a7d3f9d44b3b7a8c2e5f1a7d4e6b8c9d2e4f6a1b3d5e7f9a2b4c6d8e0f2a4"
}

source "vsphere-iso" "kali-linux" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection

  datacenter = var.vsphere_datacenter
  cluster    = var.vsphere_cluster
  datastore  = var.vsphere_datastore
  folder     = var.vsphere_folder

  vm_name = "kali-linux"
  notes   = "TrueNorth Kali Linux template built by Packer"

  guest_os_type = "debian10_64Guest"
  firmware      = "efi"

  CPUs            = 2
  RAM             = 4096
  RAM_reserve_all = false

  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }

  network_adapters {
    network      = var.vsphere_network
    network_card = "vmxnet3"
  }

  iso_url      = var.kali_iso_url
  iso_checksum = var.kali_iso_checksum

  http_directory = "../proxmox/packer/http/kali"

  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "30m"

  boot_wait = "10s"
  boot_command = [
    "<esc><wait>",
    "auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg ",
    "locale=en_US keymap=us hostname=kali domain=range.local ",
    "<enter>"
  ]

  dynamic "content_library_destination" {
    for_each = var.content_library != "" ? [1] : []
    content {
      library = var.content_library
      name    = "kali-linux"
      ovf     = true
      destroy = false
    }
  }

  convert_to_template = var.content_library == "" ? true : false
}

build {
  sources = ["source.vsphere-iso.kali-linux"]

  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      # Install open-vm-tools instead of qemu-guest-agent
      "sudo apt-get install -y open-vm-tools curl wget git",
      "sudo systemctl enable open-vm-tools",
      "sudo cloud-init clean || true",
      "sudo truncate -s 0 /etc/machine-id",
    ]
  }
}
