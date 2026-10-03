# TrueNorth Range — custom image variants (vsphere-clone).
#
# A variant is a base template plus a package list and optional scripts, baked into its
# own template so a range can use it with no egress. Each variant is one file,
# variants/<name>.pkrvars.hcl, that sets the variant_* variables (variables.pkr.hcl).
# Two generic sources consume them:
#
#   variant-windows  clone a sysprepped Windows base, get WinRM back via vSphere guest
#                    customization, choco install, run scripts, sysprep again
#   variant-linux    clone a Linux base, SSH in as the build user, apt/dnf install, run
#                    scripts, cleanup.sh
#
# Build one with build.sh, which picks the source from variant_os_family:
#   ./build.sh variant win11-analyst
# or by hand:
#   packer build -force -var-file=variants/win11-analyst.pkrvars.hcl \
#     -only=variants.vsphere-clone.variant-windows .
#
# ---- How a Windows clone gets WinRM back -------------------------------------------
# The Windows bases end with sysprep /generalize /oobe /shutdown, which disables the
# built-in Administrator (client SKUs) and leaves no password Packer knows. A plain clone
# therefore boots to a logon screen with nothing listening that Packer can use. That is
# why precomp-host / detonation-host (derived.pkr.hcl) are clone-and-template only.
#
# The variant source instead attaches a vSphere guest-customization spec (the `customize`
# block below): Windows Sysprep customization with
#   - admin_password = var.windows_admin_password, auto_logon = true (count 1). Naming
#     Administrator for auto-logon in oobeSystem re-enables the built-in account.
#   - run_once_command_list (GuiRunOnce): runs at that first auto-logon as Administrator,
#     sets the network profile Private and enables WinRM over HTTP with Basic auth, the
#     same settings the base Autounattend's FirstLogonCommands use.
# Packer's WinRM communicator then retries until those commands finish and connects.
#
# This is the same mechanism the deploy provisioner already uses on these exact templates
# (control-plane/worker/worker/provisioners/vsphere_api.py applies a Sysprep
# customization spec to every Windows clone), so if a template deploys into a range it
# also builds as a variant base. It needs VMware Tools in the base, which setup.ps1
# installs. The alternatives were worse: a guestinfo-gated first-boot hook needs every
# Windows base rebuilt and leaves the build password in the template's VMX
# extraConfig; windows_sysprep_text duplicates what windows_options already generates.
#
# After provisioning, shutdown_command runs files/windows/sysprep.ps1, so the variant is
# itself generalized and deploys exactly like a base.

locals {
  variant_notes = "TrueNorth variant ${var.variant_name}: ${var.variant_description} (cloned from ${var.variant_base} by Packer)"

  variant_windows_scripts = concat(
    ["${path.root}/files/windows/variant-packages.ps1"],
    [for s in var.variant_scripts : "${path.root}/variants/scripts/${s}"],
  )
  variant_linux_scripts = concat(
    ["${path.root}/files/linux/variant-packages.sh"],
    [for s in var.variant_scripts : "${path.root}/variants/scripts/${s}"],
  )

  variant_sudo = "echo '${var.ssh_password}' | {{ .Vars }} sudo -S -E bash '{{ .Path }}'"

  # GuiRunOnce commands for the build-time customization. Each entry is one command line.
  # Order matters: WinRM is reachable only after the last one, so Packer never logs in
  # to a half-configured listener.
  variant_winrm_runonce = [
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private\"",
    "cmd.exe /c winrm quickconfig -q -force",
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Set-Item WSMan:\\localhost\\Service\\AllowUnencrypted -Value $true; Set-Item WSMan:\\localhost\\Service\\Auth\\Basic -Value $true; Set-Item WSMan:\\localhost\\MaxTimeoutms -Value 1800000\"",
    "netsh advfirewall firewall add rule name=\"WinRM-HTTP-build\" dir=in localport=5985 protocol=tcp action=allow",
  ]
}

# ---- Windows variant -----------------------------------------------------------------
source "vsphere-clone" "variant-windows" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  template     = var.variant_base
  vm_name      = var.variant_name
  notes        = local.variant_notes
  linked_clone = false
  network      = var.build_network
  CPUs         = var.variant_cpus
  RAM          = var.variant_ram_mb
  disk_size    = var.variant_disk_mb

  customize {
    windows_options {
      computer_name         = "TN-VARIANT"
      workgroup             = "WORKGROUP"
      admin_password        = var.windows_admin_password
      auto_logon            = true
      auto_logon_count      = 1
      run_once_command_list = local.variant_winrm_runonce
    }
    network_interface {} # DHCP on build_network
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "2h"
  winrm_use_ssl  = false
  winrm_insecure = true

  # Generalize again so the variant is a clean template. sysprep.ps1 shuts the VM down.
  shutdown_command = "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\sysprep.ps1"
  shutdown_timeout = "1h"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = var.variant_name
      ovf     = true
      destroy = true
    }
  }
}

# ---- Linux variant -------------------------------------------------------------------
# The Linux bases keep the build user (ssh_username/ssh_password) and cleanup.sh only
# resets machine identity, so a clone is reachable over SSH on build_network as soon as
# it has a DHCP lease, the same way the derived Linux images work.
source "vsphere-clone" "variant-linux" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  template     = var.variant_base
  vm_name      = var.variant_name
  notes        = local.variant_notes
  linked_clone = false
  network      = var.build_network
  CPUs         = var.variant_cpus
  RAM          = var.variant_ram_mb
  disk_size    = var.variant_disk_mb

  communicator = "ssh"
  ssh_username = var.ssh_username
  ssh_password = var.ssh_password
  ssh_timeout  = "30m"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = var.variant_name
      ovf     = true
      destroy = true
    }
  }
}

build {
  name    = "variants"
  sources = ["source.vsphere-clone.variant-windows", "source.vsphere-clone.variant-linux"]

  # ---- Windows ----
  provisioner "file" {
    only        = ["vsphere-clone.variant-windows"]
    source      = "${path.root}/files/windows/sysprep-unattend.xml"
    destination = "C:/Windows/Temp/sysprep-unattend.xml"
  }
  provisioner "file" {
    only        = ["vsphere-clone.variant-windows"]
    source      = "${path.root}/files/windows/sysprep.ps1"
    destination = "C:/Windows/Temp/sysprep.ps1"
  }
  provisioner "powershell" {
    only = ["vsphere-clone.variant-windows"]
    environment_vars = [
      "DEPOT_URL=${var.depot_url}",
      "VARIANT_NAME=${var.variant_name}",
      "VARIANT_CHOCO_PACKAGES=${join(";", var.variant_choco_packages)}",
    ]
    scripts = local.variant_windows_scripts
  }
  # Installers (Office, .NET, VC runtimes) often leave a pending reboot; take it before
  # sysprep, which refuses to run with one pending.
  provisioner "windows-restart" {
    only            = ["vsphere-clone.variant-windows"]
    restart_timeout = "30m"
  }

  # ---- Linux ----
  provisioner "shell" {
    only = ["vsphere-clone.variant-linux"]
    environment_vars = [
      "DEPOT_URL=${var.depot_url}",
      "VARIANT_NAME=${var.variant_name}",
      "VARIANT_PACKAGES=${join(" ", var.variant_apt_packages)}",
    ]
    # Run as root; the build user's sudo may want its password. -E keeps the env above.
    execute_command = local.variant_sudo
    scripts         = local.variant_linux_scripts
  }
  provisioner "shell" {
    only            = ["vsphere-clone.variant-linux"]
    execute_command = local.variant_sudo
    script          = "${path.root}/files/linux/cleanup.sh"
  }
}
