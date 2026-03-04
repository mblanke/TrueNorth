# TrueNorth Range — Packer template for Windows 10 Enterprise workstation image
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "win10_iso_file" {
  type    = string
  default = "local:iso/Win10_Enterprise_Eval_x64.iso"
}

variable "virtio_iso_file_win10" {
  type    = string
  default = "local:iso/virtio-win.iso"
}

source "proxmox-iso" "windows-10-workstation" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  password                 = var.proxmox_password
  node                     = var.proxmox_node
  insecure_skip_tls_verify = true

  iso_file         = var.win10_iso_file
  iso_storage_pool = var.iso_storage_pool

  additional_iso_files {
    device           = "sata1"
    iso_file         = var.virtio_iso_file_win10
    iso_storage_pool = var.iso_storage_pool
    unmount          = true
  }

  vm_name              = "win10-workstation"
  template_description = "Windows 10 Enterprise workstation template for TrueNorth Range"

  cores   = 2
  memory  = 4096
  os      = "win10"
  machine = "q35"
  bios    = "ovmf"

  scsi_controller = "virtio-scsi-single"

  disks {
    disk_size    = "50G"
    storage_pool = var.vm_storage_pool
    type         = "scsi"
  }

  network_adapters {
    model  = "virtio"
    bridge = "vmbr0"
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = "Packer!"
  winrm_timeout  = var.winrm_timeout
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait = "5s"
  boot_command = [
    "<spacebar>"
  ]

  http_directory = "http/win10"
}

build {
  sources = ["source.proxmox-iso.windows-10-workstation"]

  # Enable RDP and disable firewall for provisioning
  provisioner "powershell" {
    inline = [
      "Set-ExecutionPolicy Bypass -Scope Process -Force",
      "Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name 'fDenyTSConnections' -Value 0",
      "Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'",
      "Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False",
    ]
  }

  # Install Chocolatey
  provisioner "powershell" {
    inline = [
      "Set-ExecutionPolicy Bypass -Scope Process -Force",
      "[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072",
      "Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))",
    ]
  }

  # Install productivity and realism software
  provisioner "powershell" {
    inline = [
      "choco install -y googlechrome",
      "choco install -y libreoffice-fresh",
      "choco install -y 7zip",
      "choco install -y notepadplusplus",
      "choco install -y vlc",
      "choco install -y adobereader",
    ]
  }

  # Install security monitoring tools
  provisioner "powershell" {
    inline = [
      "choco install -y sysmon --params '/AcceptEula'",
      "choco install -y winlogbeat",
      "choco install -y qemu-guest-agent",
    ]
  }

  # Configure Sysmon
  provisioner "powershell" {
    inline = [
      "$sysmonDir = 'C:\\ProgramData\\sysmon'",
      "New-Item -ItemType Directory -Force -Path $sysmonDir | Out-Null",
      "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/SwiftOnSecurity/sysmon-config/master/sysmonconfig-export.xml' -OutFile \"$sysmonDir\\sysmonconfig.xml\"",
      "& 'C:\\ProgramData\\chocolatey\\lib\\sysmon\\tools\\Sysmon64.exe' -accepteula -i \"$sysmonDir\\sysmonconfig.xml\"",
    ]
  }

  # Configure Winlogbeat
  provisioner "powershell" {
    inline = [
      "Set-Service -Name winlogbeat -StartupType Automatic",
    ]
  }

  # Create trainee user account
  provisioner "powershell" {
    inline = [
      "# Create standard trainee user",
      "net user trainee 'Trainee@123' /add /fullname:'Range Trainee' /comment:'Standard training user account'",
      "net localgroup Users trainee /add",
      "# Admin account is already 'Administrator' from unattend",
    ]
  }

  # Install cloudbase-init
  provisioner "powershell" {
    inline = [
      "Invoke-WebRequest -Uri 'https://cloudbase.it/downloads/CloudbaseInitSetup_Stable_x64.msi' -OutFile 'C:\\Windows\\Temp\\CloudbaseInit.msi'",
      "Start-Process msiexec.exe -ArgumentList '/i C:\\Windows\\Temp\\CloudbaseInit.msi /qn /norestart' -Wait",
    ]
  }

  # Re-enable firewall and sysprep
  provisioner "powershell" {
    inline = [
      "Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True",
      "& \"$env:SystemRoot\\System32\\Sysprep\\sysprep.exe\" /oobe /generalize /shutdown /quiet",
    ]
  }
}