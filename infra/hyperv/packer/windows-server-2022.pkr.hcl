# TrueNorth Range — Packer template for Windows Server 2022 on Hyper-V
# Requires: Windows build host with Hyper-V role, elevated PowerShell.
packer {
  required_plugins {
    hyperv = {
      version = ">= 1.1.3"
      source  = "github.com/hashicorp/hyperv"
    }
  }
}

variable "win2022_iso_path" {
  description = "Local path to the Windows Server 2022 evaluation ISO"
  type        = string
  default     = "C:\\ISOs\\SERVER_EVAL_x64FRE_en-us.iso"
}

source "hyperv-iso" "windows-server-2022" {
  vm_name     = "win2022-server"
  generation  = 2
  switch_name = var.hyperv_switch

  cpus     = 4
  ram_size = 4096

  disk_size       = 65536
  disk_block_size = 1

  iso_url      = var.win2022_iso_path
  iso_checksum = "none"

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

  shutdown_command = "shutdown /s /t 10"
  output_directory = "${var.output_base_dir}\\win2022-server"
  headless         = true

  enable_secure_boot    = false
  enable_dynamic_memory = false
}

build {
  sources = ["source.hyperv-iso.windows-server-2022"]

  provisioner "powershell" {
    inline = [
      "Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False",
      "Enable-PSRemoting -Force",
      # Install Integration Services if not already present
      "Add-WindowsFeature Hyper-V-Tools -ErrorAction SilentlyContinue | Out-Null",
    ]
  }

  provisioner "powershell" {
    inline = [
      "$url = 'https://download.sysinternals.com/files/Sysmon.zip'",
      "Invoke-WebRequest -Uri $url -OutFile C:\\sysmon.zip -UseBasicParsing",
      "Expand-Archive C:\\sysmon.zip -DestinationPath C:\\sysmon -Force",
      "C:\\sysmon\\Sysmon64.exe -accepteula -i || Write-Host 'Sysmon already installed'",
    ]
  }

  post-processor "shell-local" {
    only_on = ["windows"]
    inline = [
      "Copy-Item -Path \"${var.output_base_dir}\\win2022-server\\Virtual Hard Disks\\*.vhdx\" -Destination \"${var.output_base_dir}\\win2022-server.vhdx\" -Force"
    ]
    execute_command = ["powershell.exe", "{{.Vars}}", "{{.Script}}"]
  }
}
