# TrueNorth Range — Windows golden templates (vsphere-iso).
# Sources: srv2022, srv2025, srv2019, srv2016, win10-22h2, win10-ltsc, win11-24h2.
# All sources share the Autounattend templatefile and the setup.ps1 / sysprep.ps1
# provisioners. Build one with:  packer build -only='windows.vsphere-iso.<id>' .
# (or ./build.sh <id>). A bare 'vsphere-iso.<id>' matches nothing: the build is named.
#
# ISOs are pre-staged on a host-local datastore, so every source pins host/datastore to
# the ISO host (var.vsphere_host / var.iso_datastore). Never iso_url downloads.
#
# Highest build risk: win11-24h2 and srv2025 use the new Windows Setup engine whose
# unattended UEFI partitioning is fragile (see README "Windows 11 / Server 2025"). The
# proven 4-partition layout in autounattend.pkrtpl.hcl is the best-effort automated path;
# the manual-install + sysprep fallback is documented in the README.

# ---- Server 2022 (classic engine; most reliable Windows build) ----------------------
source "vsphere-iso" "srv2022" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "srv2022"
  notes         = "TrueNorth golden: Windows Server 2022 (built by Packer)"
  guest_os_type = "windows2019srvNext64Guest"
  firmware      = "efi"

  CPUs                 = 4
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_srv2022}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = "Windows Server 2022 Standard (Desktop Experience)"
      product_key         = var.gvlk_srv2022
      computer_name       = "SRV2022"
      admin_password      = var.windows_admin_password
      labconfig           = false
      bypass_nro          = false
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2022"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Server 2025 (new engine; higher risk) ------------------------------------------
source "vsphere-iso" "srv2025" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "srv2025"
  notes         = "TrueNorth golden: Windows Server 2025 (built by Packer)"
  guest_os_type = "windows2019srvNext64Guest"
  firmware      = "efi"

  CPUs                 = 4
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_srv2025}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = "Windows Server 2025 Standard (Desktop Experience)"
      product_key         = var.gvlk_srv2025
      computer_name       = "SRV2025"
      admin_password      = var.windows_admin_password
      labconfig           = false
      bypass_nro          = true
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2025"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Server 2019 (eval media; ISO must be uploaded first) ---------------------------
source "vsphere-iso" "srv2019" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "srv2019"
  notes         = "TrueNorth golden: Windows Server 2019 (built by Packer)"
  guest_os_type = "windows2019srv_64Guest"
  firmware      = "efi"

  CPUs                 = 4
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_srv2019}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = "Windows Server 2019 Standard (Desktop Experience)"
      product_key         = ""
      computer_name       = "SRV2019"
      admin_password      = var.windows_admin_password
      labconfig           = false
      bypass_nro          = false
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2019"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Server 2016 (eval media; ISO must be uploaded first) ---------------------------
source "vsphere-iso" "srv2016" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "srv2016"
  notes         = "TrueNorth golden: Windows Server 2016 (built by Packer)"
  guest_os_type = "windows9Server64Guest"
  firmware      = "efi"

  CPUs                 = 4
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_srv2016}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = "Windows Server 2016 Standard (Desktop Experience)"
      product_key         = ""
      computer_name       = "SRV2016"
      admin_password      = var.windows_admin_password
      labconfig           = false
      bypass_nro          = false
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2016"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Windows 10 22H2 Enterprise (eval media; ISO must be uploaded first) ------------
source "vsphere-iso" "win10-22h2" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "win10-22h2"
  notes         = "TrueNorth golden: Windows 10 22H2 Enterprise (built by Packer)"
  guest_os_type = "windows9_64Guest"
  firmware      = "efi"

  CPUs                 = 2
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 40960
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_win10}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = "Windows 10 Enterprise Evaluation"
      product_key         = ""
      computer_name       = "WIN10"
      admin_password      = var.windows_admin_password
      labconfig           = false
      bypass_nro          = false
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "win10-22h2"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Windows 10 Enterprise LTSC 2021 (classic engine; ISO must be uploaded first) ---
# Same build path as win10-22h2. LTSC has no Store/inbox apps and a 10-year servicing
# channel, which suits long-lived victim workstations. VL media need gvlk_win10_ltsc;
# eval media need gvlk_win10_ltsc = "" and the eval win10_ltsc_image_name.
source "vsphere-iso" "win10-ltsc" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "win10-ltsc"
  notes         = "TrueNorth golden: Windows 10 Enterprise LTSC 2021 (built by Packer)"
  guest_os_type = "windows9_64Guest"
  firmware      = "efi"

  CPUs                 = 2
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 61440
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_win10_ltsc}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = var.win10_ltsc_image_name
      product_key         = var.gvlk_win10_ltsc
      computer_name       = "WIN10LTSC"
      admin_password      = var.windows_admin_password
      labconfig           = false
      bypass_nro          = false
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "win10-ltsc"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Windows 11 24H2 Pro (new engine; vTPM optional; 64 GB; highest risk) -----------
source "vsphere-iso" "win11-24h2" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  vm_name       = "win11-24h2"
  notes         = "TrueNorth golden: Windows 11 24H2 Pro (built by Packer)"
  guest_os_type = "windows11_64Guest"
  firmware      = "efi-secure"
  vTPM          = var.win11_vtpm

  CPUs                 = 2
  RAM                  = 4096
  disk_controller_type = ["pvscsi"]
  storage {
    disk_size             = 65536
    disk_thin_provisioned = true
  }
  network_adapters {
    network      = var.build_network
    network_card = "vmxnet3"
  }

  iso_paths = ["${local.iso_dir}/${var.iso_win11_24h2}", var.vmtools_iso_path]
  cd_label  = "PROVISION"
  cd_content = {
    "autounattend.xml" = templatefile("${path.root}/files/windows/autounattend.pkrtpl.hcl", {
      image_name          = "Windows 11 Pro"
      product_key         = var.gvlk_win11_pro
      computer_name       = "WIN11"
      admin_password      = var.windows_admin_password
      labconfig           = !var.win11_vtpm
      bypass_nro          = true
      pvscsi_driver_paths = local.pvscsi_driver_paths
    })
  }

  communicator   = "winrm"
  winrm_username = "Administrator"
  winrm_password = var.windows_admin_password
  winrm_timeout  = "3h"
  winrm_use_ssl  = false
  winrm_insecure = true

  boot_wait    = "3s"
  boot_command = ["<spacebar>"]

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "win11-24h2"
      ovf     = true
      destroy = true
    }
  }
}

# ---- Shared build block for every Windows source ------------------------------------
build {
  name = "windows"
  sources = [
    "source.vsphere-iso.srv2022",
    "source.vsphere-iso.srv2025",
    "source.vsphere-iso.srv2019",
    "source.vsphere-iso.srv2016",
    "source.vsphere-iso.win10-22h2",
    "source.vsphere-iso.win10-ltsc",
    "source.vsphere-iso.win11-24h2",
  ]

  provisioner "file" {
    source      = "${path.root}/files/windows/sysprep-unattend.xml"
    destination = "C:/Windows/Temp/sysprep-unattend.xml"
  }

  provisioner "powershell" {
    environment_vars = ["DEPOT_URL=${var.depot_url}"]
    script           = "${path.root}/files/windows/setup.ps1"
  }

  # Final: generalize and shut down. Keep this last.
  provisioner "powershell" {
    script = "${path.root}/files/windows/sysprep.ps1"
  }
}
