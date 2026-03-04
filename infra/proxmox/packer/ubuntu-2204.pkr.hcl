# TrueNorth Range — Packer template for Ubuntu 22.04 cloud-init base image
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "proxmox_url" {
  type    = string
  default = "https://pve:8006/api2/json"
}

variable "proxmox_username" {
  type    = string
  default = "root@pam"
}

variable "proxmox_password" {
  type      = string
  sensitive = true
}

variable "proxmox_node" {
  type    = string
  default = "pve"
}

source "proxmox-iso" "ubuntu-2204" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  password                 = var.proxmox_password
  node                     = var.proxmox_node
  insecure_skip_tls_verify = true

  iso_file    = "local:iso/ubuntu-22.04.3-live-server-amd64.iso"
  iso_storage_pool = "local"

  vm_name              = "ubuntu-2204-cloud"
  template_description = "Ubuntu 22.04 LTS cloud-init template for TrueNorth Range"

  cores   = 2
  memory  = 2048
  os      = "l26"
  machine = "q35"
  bios    = "ovmf"

  scsi_controller = "virtio-scsi-single"

  disks {
    disk_size    = "32G"
    storage_pool = "local-lvm"
    type         = "scsi"
  }

  network_adapters {
    model  = "virtio"
    bridge = "vmbr0"
  }

  cloud_init              = true
  cloud_init_storage_pool = "local-lvm"

  ssh_username = "ubuntu"
  ssh_password = "ubuntu"
  ssh_timeout  = "20m"

  boot_command = [
    "<esc><esc><esc><esc>e<wait>",
    "<down><down><down><end>",
    " autoinstall ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/",
    "<f10>"
  ]

  http_directory = "http"
}

build {
  sources = ["source.proxmox-iso.ubuntu-2204"]

  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      "sudo apt-get install -y qemu-guest-agent cloud-init curl wget git",
      "sudo apt-get install -y auditd sysstat net-tools",
      "sudo systemctl enable qemu-guest-agent",
      "sudo cloud-init clean",
      "sudo truncate -s 0 /etc/machine-id",
    ]
  }

  provisioner "shell" {
    inline = [
      "# Install Sysmon for Linux (optional - for telemetry)",
      "wget -q https://packages.microsoft.com/config/ubuntu/22.04/packages-microsoft-prod.deb -O /tmp/packages-microsoft-prod.deb",
      "sudo dpkg -i /tmp/packages-microsoft-prod.deb || true",
      "sudo apt-get update",
      "sudo apt-get install -y sysinternalsebpf sysmonforlinux || echo 'Sysmon install skipped'",
    ]
  }
}
