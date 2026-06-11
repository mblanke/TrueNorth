# TrueNorth Range — Packer template for Windows Server 2022 on vSphere
packer {
  required_plugins {
    vsphere = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/vsphere"
    }
  }
}

variable "win2022_iso_url" {
  description = "URL or datastore path to the Windows Server 2022 evaluation ISO"
  type        = string
  default     = "[datastore1] iso/SERVER_EVAL_x64FRE_en-us.iso"
}

variable "win2022_iso_checksum" {
  type    = string
  default = "sha256:3e4fa961b6c6f6990e7e5e8d8b4bab7ff60f85cffce88be3de7b22e7e0085f6e"
}

variable "winrm_username" {
  type    = string
  default = "Administrator"
}

variable "winrm_password" {
  type      = string
  sensitive = true
  default   = "Packer!"
}

source "vsphere-iso" "windows-server-2022" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection

  datacenter = var.vsphere_datacenter
  cluster    = var.vsphere_cluster
  datastore  = var.vsphere_datastore
  folder     = var.vsphere_folder

  vm_name    = "win2022-server"
  notes      = "TrueNorth Windows Server 2022 template built by Packer"

  guest_os_type = "windows2019srvNext64Guest"
  firmware      = "efi"

  CPUs            = 4
  RAM             = 4096
  RAM_reserve_all = false

  disk_controller_type = ["lsilogic-sas"]
  storage {
    disk_size             = 65536
    disk_thin_provisioned = true
  }

  network_adapters {
    network      = var.vsphere_network
    network_card = "vmxnet3"
  }

  iso_url      = var.win2022_iso_url
  iso_checksum = var.win2022_iso_checksum

  # Autounattend from the shared http directory
  floppy_files = [
    "../proxmox/packer/http/win2022/Autounattend.xml",
    "../proxmox/packer/http/win2022/setup.ps1",
  ]

  communicator   = "winrm"
  winrm_username = var.winrm_username
  winrm_password = var.winrm_password
  winrm_timeout  = "45m"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "5s"
  boot_command = ["<spacebar>"]

  dynamic "content_library_destination" {
    for_each = var.content_library != "" ? [1] : []
    content {
      library = var.content_library
      name    = "win2022-server"
      ovf     = true
      destroy = false
    }
  }

  convert_to_template = var.content_library == "" ? true : false
}

build {
  sources = ["source.vsphere-iso.windows-server-2022"]

  provisioner "powershell" {
    inline = [
      "Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False",
      "Enable-PSRemoting -Force",
      "Install-WindowsFeature -Name 'RSAT-AD-Tools' -IncludeAllSubFeature | Out-Null",
      # Install VMware Tools silently (assumes VMware Tools ISO is mounted or available)
      "Start-Process -FilePath 'setup64.exe' -ArgumentList '/S /v/qn' -Wait -ErrorAction SilentlyContinue",
    ]
  }

  provisioner "powershell" {
    inline = [
      "# Sysmon",
      "$url = 'https://download.sysinternals.com/files/Sysmon.zip'",
      "Invoke-WebRequest -Uri $url -OutFile C:\\sysmon.zip -UseBasicParsing",
      "Expand-Archive C:\\sysmon.zip -DestinationPath C:\\sysmon -Force",
      "C:\\sysmon\\Sysmon64.exe -accepteula -i || Write-Host 'Sysmon already installed'",
    ]
  }
}
