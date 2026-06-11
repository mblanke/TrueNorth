# TrueNorth Range — Packer template for Ubuntu 22.04 on Hyper-V
# Requires: Windows build host with Hyper-V role, elevated PowerShell.
packer {
  required_plugins {
    hyperv = {
      version = ">= 1.1.3"
      source  = "github.com/hashicorp/hyperv"
    }
  }
}

source "hyperv-iso" "ubuntu-2204" {
  vm_name          = "ubuntu-2204-cloud"
  generation       = var.hyperv_generation
  switch_name      = var.hyperv_switch

  cpus   = 2
  ram_size = 2048

  disk_size        = 32768
  disk_block_size  = 1

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

  ssh_username     = var.ssh_username
  ssh_password     = var.ssh_password
  ssh_timeout      = "25m"
  shutdown_command = "echo '${var.ssh_password}' | sudo -S shutdown -P now"

  output_directory = "${var.output_base_dir}\\ubuntu-2204-cloud"
  headless         = true

  enable_secure_boot    = false
  enable_dynamic_memory = false
}

build {
  sources = ["source.hyperv-iso.ubuntu-2204"]

  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      # Install Hyper-V Integration Services (Linux Enlightenments)
      "sudo apt-get install -y hyperv-daemons cloud-init curl wget git",
      "sudo apt-get install -y auditd sysstat net-tools",
      "sudo systemctl enable hv-kvp-daemon hv-vss-daemon || true",
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

  # Export the finished VHDX to the output_base_dir so HypervProvisioner can use it
  post-processor "shell-local" {
    only_on = ["windows"]
    inline = [
      "Copy-Item -Path \"${var.output_base_dir}\\ubuntu-2204-cloud\\Virtual Hard Disks\\*.vhdx\" -Destination \"${var.output_base_dir}\\ubuntu-2204.vhdx\" -Force"
    ]
    execute_command = ["powershell.exe", "{{.Vars}}", "{{.Script}}"]
  }
}
