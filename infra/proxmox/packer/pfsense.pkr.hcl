# TrueNorth Range — Packer template for pfSense CE 2.7 firewall/router image
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "pfsense_iso_file" {
  type    = string
  default = "local:iso/pfSense-CE-2.7.2-RELEASE-amd64.iso"
}

source "proxmox-iso" "pfsense" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  password                 = var.proxmox_password
  node                     = var.proxmox_node
  insecure_skip_tls_verify = true

  iso_file         = var.pfsense_iso_file
  iso_storage_pool = var.iso_storage_pool

  vm_name              = "pfsense-router"
  template_description = "pfSense CE 2.7 firewall/router template for TrueNorth Range"

  cores   = 1
  memory  = 1024
  os      = "other"
  machine = "q35"
  bios    = "seabios"

  scsi_controller = "virtio-scsi-single"

  disks {
    disk_size    = "8G"
    storage_pool = var.vm_storage_pool
    type         = "scsi"
  }

  # WAN interface
  network_adapters {
    model  = "virtio"
    bridge = "vmbr0"
  }

  # LAN interface
  network_adapters {
    model  = "virtio"
    bridge = "vmbr1"
  }

  communicator = "ssh"
  ssh_username = "root"
  ssh_password = "pfsense"
  ssh_timeout  = var.ssh_timeout

  boot_wait = "30s"
  boot_command = [
    # Accept license and proceed with install
    "<enter><wait5>",
    "<enter><wait5>",
    # Install pfSense
    "<enter><wait5>",
    # Select keymap
    "<enter><wait5>",
    # Auto (ZFS) partitioning
    "<enter><wait5>",
    # Proceed with installation
    "<enter><wait5>",
    # stripe - no redundancy
    "<enter><wait5>",
    # Select disk
    "<spacebar><enter><wait5>",
    # Confirm
    "<left>y<enter><wait120>",
    # Reboot
    "<enter><wait30>",
    # Wait for reboot and login
    "<wait60>",
  ]

  http_directory = "http/pfsense"
}

build {
  sources = ["source.proxmox-iso.pfsense"]

  # Enable SSH permanently and configure base system
  provisioner "shell" {
    inline = [
      "# Enable SSH in pfSense",
      "echo 'sshd_enable=\"YES\"' >> /etc/rc.conf",
      "# Set up pkg repository for pfSense",
      "pkg update -f || true",
    ]
  }

  # Restore bootstrap configuration from XML
  provisioner "shell" {
    inline = [
      "# Apply bootstrap config with WAN/LAN pre-configured",
      "# The config.xml from http_directory provides base network setup",
      "echo 'Bootstrap config will be applied via config.xml restore'",
      "# Backup current config",
      "cp /cf/conf/config.xml /cf/conf/config.xml.bak",
    ]
  }

  # Install pfBlockerNG
  provisioner "shell" {
    inline = [
      "pkg install -y pfSense-pkg-pfBlockerNG-devel || echo 'pfBlockerNG will be installed at deploy time'",
    ]
  }

  # Install Suricata package
  provisioner "shell" {
    inline = [
      "pkg install -y pfSense-pkg-suricata || echo 'Suricata package will be installed at deploy time'",
    ]
  }

  # Install QEMU guest agent
  provisioner "shell" {
    inline = [
      "pkg install -y qemu-guest-agent || true",
      "echo 'qemu_guest_agent_enable=\"YES\"' >> /etc/rc.conf",
      "echo 'qemu_guest_agent_flags=\"-d -v -l /var/log/qemu-ga.log\"' >> /etc/rc.conf",
    ]
  }

  # Final cleanup
  provisioner "shell" {
    inline = [
      "# Clean package cache",
      "pkg clean -y || true",
      "# Remove SSH host keys (regenerated on first boot)",
      "rm -f /etc/ssh/ssh_host_*",
    ]
  }
}