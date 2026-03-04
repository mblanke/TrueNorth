# TrueNorth Range — Packer template for Windows Server 2022 base image
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "win2022_iso_file" {
  type    = string
  default = "local:iso/SERVER_EVAL_x64FRE_en-us.iso"
}

variable "virtio_iso_file" {
  type    = string
  default = "local:iso/virtio-win.iso"
}

source "proxmox-iso" "windows-server-2022" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  password                 = var.proxmox_password
  node                     = var.proxmox_node
  insecure_skip_tls_verify = true

  iso_file         = var.win2022_iso_file
  iso_storage_pool = var.iso_storage_pool

  additional_iso_files {
    device           = "sata1"
    iso_file         = var.virtio_iso_file
    iso_storage_pool = var.iso_storage_pool
    unmount          = true
  }

  vm_name              = "win2022-server"
  template_description = "Windows Server 2022 template for TrueNorth Range"

  cores   = 4
  memory  = 4096
  os      = "win11"
  machine = "q35"
  bios    = "ovmf"

  scsi_controller = "virtio-scsi-single"

  disks {
    disk_size    = "64G"
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

  http_directory = "http/win2022"
}

build {
  sources = ["source.proxmox-iso.windows-server-2022"]

  # Enable RDP and disable firewall for provisioning
  provisioner "powershell" {
    inline = [
      "Set-ExecutionPolicy Bypass -Scope Process -Force",
      "# Enable RDP",
      "Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name 'fDenyTSConnections' -Value 0",
      "Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'",
      "# Disable firewall during provisioning",
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

  # Install security tooling via Chocolatey
  provisioner "powershell" {
    inline = [
      "choco install -y sysmon --params '/AcceptEula'",
      "choco install -y winlogbeat",
      "choco install -y qemu-guest-agent",
    ]
  }

  # Configure Sysmon with SwiftOnSecurity config
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
      "$wlbDir = 'C:\\ProgramData\\winlogbeat'",
      "# Winlogbeat config will be overridden by Ansible post-deploy",
      "# Start the service so it's ready",
      "Set-Service -Name winlogbeat -StartupType Automatic",
    ]
  }

  # Enable Windows Defender features
  provisioner "powershell" {
    inline = [
      "Set-MpPreference -DisableRealtimeMonitoring $false",
      "Update-MpSignature -ErrorAction SilentlyContinue",
      "Enable-WindowsOptionalFeature -Online -FeatureName Windows-Defender -All -NoRestart -ErrorAction SilentlyContinue",
    ]
  }

  # Install cloudbase-init for cloud-init support
  provisioner "powershell" {
    inline = [
      "Invoke-WebRequest -Uri 'https://cloudbase.it/downloads/CloudbaseInitSetup_Stable_x64.msi' -OutFile 'C:\\Windows\\Temp\\CloudbaseInit.msi'",
      "Start-Process msiexec.exe -ArgumentList '/i C:\\Windows\\Temp\\CloudbaseInit.msi /qn /norestart' -Wait",
    ]
  }

  # Re-enable firewall and clean up
  provisioner "powershell" {
    inline = [
      "Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True",
      "# Generalize for template",
      "& \"$env:SystemRoot\\System32\\Sysprep\\sysprep.exe\" /oobe /generalize /shutdown /quiet",
    ]
  }
}