# TrueNorth Range — Packer template for Kali Linux attack platform image
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "kali_iso_file" {
  type    = string
  default = "local:iso/kali-linux-2024.1-installer-amd64.iso"
}

source "proxmox-iso" "kali-linux" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  password                 = var.proxmox_password
  node                     = var.proxmox_node
  insecure_skip_tls_verify = true

  iso_file         = var.kali_iso_file
  iso_storage_pool = var.iso_storage_pool

  vm_name              = "kali-linux"
  template_description = "Kali Linux 2024.1 attack platform template for TrueNorth Range"

  cores   = 2
  memory  = 4096
  os      = "l26"
  machine = "q35"
  bios    = "ovmf"

  scsi_controller = "virtio-scsi-single"

  disks {
    disk_size    = "40G"
    storage_pool = var.vm_storage_pool
    type         = "scsi"
  }

  network_adapters {
    model  = "virtio"
    bridge = "vmbr0"
  }

  ssh_username = "kali"
  ssh_password = "kali"
  ssh_timeout  = var.ssh_timeout

  boot_wait = "10s"
  boot_command = [
    "<esc><wait>",
    "auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg ",
    "locale=en_US keymap=us hostname=kali domain=range.local ",
    "<enter>"
  ]

  http_directory = "http/kali"
}

build {
  sources = ["source.proxmox-iso.kali-linux"]

  # Update system and install full Kali metapackage
  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      "sudo DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y",
      "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y kali-linux-default",
    ]
  }

  # Install QEMU guest agent and cloud-init
  provisioner "shell" {
    inline = [
      "sudo apt-get install -y qemu-guest-agent cloud-init curl wget git",
      "sudo systemctl enable qemu-guest-agent",
    ]
  }

  # Install CALDERA agent
  provisioner "shell" {
    inline = [
      "sudo mkdir -p /opt/caldera-agent",
      "cd /opt/caldera-agent",
      "sudo wget -q https://raw.githubusercontent.com/mitre/caldera/master/plugins/sandcat/payloads/sandcat.go-linux -O sandcat || echo 'CALDERA agent download placeholder'",
      "sudo chmod +x /opt/caldera-agent/sandcat || true",
      "# CALDERA server address will be configured via Ansible post-deploy",
    ]
  }

  # Install Filebeat for log shipping
  provisioner "shell" {
    inline = [
      "wget -qO - https://artifacts.elastic.co/GPG-KEY-elasticsearch | sudo gpg --dearmor -o /usr/share/keyrings/elastic-keyring.gpg",
      "echo 'deb [signed-by=/usr/share/keyrings/elastic-keyring.gpg] https://artifacts.elastic.co/packages/8.x/apt stable main' | sudo tee /etc/apt/sources.list.d/elastic-8.x.list",
      "sudo apt-get update",
      "sudo apt-get install -y filebeat",
      "sudo systemctl enable filebeat",
    ]
  }

  # Create trainee user with sudo
  provisioner "shell" {
    inline = [
      "sudo useradd -m -s /bin/bash -G sudo trainee",
      "echo 'trainee:Trainee@123' | sudo chpasswd",
      "echo 'trainee ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/trainee",
      "sudo chmod 0440 /etc/sudoers.d/trainee",
    ]
  }

  # Clean up for template
  provisioner "shell" {
    inline = [
      "sudo cloud-init clean",
      "sudo truncate -s 0 /etc/machine-id",
      "sudo apt-get autoremove -y",
      "sudo apt-get clean",
    ]
  }
}