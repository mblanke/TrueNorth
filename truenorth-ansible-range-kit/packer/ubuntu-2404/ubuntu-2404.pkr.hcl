# TrueNorth golden image: Ubuntu 24.04 on vSphere.
# Inputs are pinned (ISO URL + sha256). Output: vCenter template tmpl-ubuntu2404-<build_id>.
# After the build: run the acceptance checks (reference/golden-images.md), record provenance
# in config/templates.yml, then promote with playbooks/template_promote.yml.
#
# LAB-VALIDATE: autoinstall, the guest ops account and the hardening baseline are TBD.

packer {
  required_plugins {
    vsphere = {
      source  = "github.com/hashicorp/vsphere"
      version = "~> 1.4"
    }
  }
}

variable "vcenter_server"   { type = string }
variable "vcenter_username" { type = string }
variable "vcenter_password" {
  type      = string
  sensitive = true
}
variable "datacenter"     { type = string }
variable "cluster"        { type = string }
variable "datastore"      { type = string }
variable "build_network"  { type = string }   # temporary build network with access to the ISO/package mirror
variable "folder" {
  type = string
  default = "TrueNorth/Templates"
}
variable "build_id"       { type = string }   # CI run id; becomes part of the template name
variable "iso_url"        { type = string }   # internal mirror path
variable "iso_sha256"     { type = string }   # REQUIRED - pinned; recorded as source_sha256
variable "hw_version"     {
  type    = number
  default = 20   # vmx-20 = ESXi 8.0, vmx-21 = 8.0 U2, vmx-22 = ESX 9.0 (will not run on ESX 8 hosts)
}
variable "guest_user" {
  type = string
  default = "tnops"
}
variable "guest_password" {
  type      = string
  sensitive = true
}

source "vsphere-iso" "ubuntu" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = false
  datacenter          = var.datacenter
  cluster             = var.cluster
  datastore           = var.datastore
  folder              = var.folder

  vm_name              = "tmpl-ubuntu2404-${var.build_id}"
  guest_os_type        = "ubuntu64Guest"
  vm_version           = var.hw_version
  CPUs                 = 2
  RAM                  = 2048
  firmware             = "efi"
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 20480
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"                  # must match nic_device_type in config/templates.yml
  }

  iso_url      = var.iso_url
  iso_checksum = "sha256:${var.iso_sha256}"
  cd_content   = { "meta-data" = "", "user-data" = templatefile("${path.root}/http/user-data.pkrtpl", {
    guest_user = var.guest_user, guest_password_hash = bcrypt(var.guest_password)
  }) }
  cd_label     = "cidata"
  boot_wait    = "5s"
  boot_command = ["<wait>e<down><down><down><end> autoinstall ds=nocloud;<F10>"]

  ssh_username = var.guest_user
  ssh_password = var.guest_password
  ssh_timeout  = "45m"

  notes               = "TrueNorth golden image | build ${var.build_id} | iso sha256 ${var.iso_sha256}"
  convert_to_template = true
}

build {
  sources = ["source.vsphere-iso.ubuntu"]

  # Pristine: generalise before templating. Each clone must get its own identity.
  provisioner "shell" {
    execute_command = "echo '${var.guest_password}' | sudo -S bash -euo pipefail '{{ .Path }}'"
    inline = [
      "apt-get -y install open-vm-tools",
      "systemctl enable open-vm-tools",
      "apt-get -y autoremove --purge && apt-get clean",
      "rm -f /etc/netplan/*.yaml && rm -rf /var/lib/dhcp/* /var/lib/NetworkManager/*lease*",
      "rm -f /etc/ssh/ssh_host_*",
      "printf '[Unit]\\nDescription=Regenerate SSH host keys\\nBefore=ssh.service\\nConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\\n[Service]\\nType=oneshot\\nExecStart=/usr/bin/ssh-keygen -A\\n[Install]\\nWantedBy=multi-user.target\\n' > /etc/systemd/system/tn-ssh-keys.service",
      "systemctl enable tn-ssh-keys.service",
      "cloud-init clean --logs --seed || true",
      "truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id && ln -sf /etc/machine-id /var/lib/dbus/machine-id",
      "find /var/log -type f -exec truncate -s 0 {} +",
      "rm -rf /tmp/* /var/tmp/* /root/.bash_history /home/*/.bash_history",
    ]
  }

  post-processor "manifest" {
    output     = "packer-manifest-ubuntu2404.json"
    strip_path = true
    custom_data = {
      build_id      = var.build_id
      source_sha256 = var.iso_sha256
      alias         = "ubuntu2404"
    }
  }
}
