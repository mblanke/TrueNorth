# TrueNorth Range — Windows Server role images (vsphere-clone of srv2022).
#
# The images control-plane/api/app/windows_roles.py names for its "image" roles:
#   srv2022-exchange2019   Exchange Server 2019 CU15
#   srv2022-sharepointse   SharePoint Server Subscription Edition
#   srv2022-sharepoint2019 SharePoint Server 2019 (Large Enterprise; prerequisites installed one by one)
#   srv2022-mecm           Configuration Manager current branch + SQL Server 2022 + ADK/WinPE
#
# Each clones var.base_srv2022_template, gets WinRM back through the same vSphere guest
# customization the variants use (variants.pkr.hcl, local.variant_winrm_runonce), mounts
# the product media as extra CD-ROMs, and runs files/windows/role-image.ps1 in two passes
# with a reboot between. Prerequisites that are not on the product media come from the
# depot's raw "installers" repository, <depot_url>/installers/roles/<image>/ (README,
# "Role images"). sysprep.ps1 generalizes the result like every other Windows template.
#
# What is NOT done at build time, and why (every one of these needs the range's domain).
# TrueNorth runs it at deploy (control-plane/worker/worker/provisioners/vsphere_products.py)
# on a clone whose C:\TrueNorth\role-image.json names the image: join the range domain,
# then the product setup below, as the domain Administrator.
#   exchange2019  Exchange Setup (/PrepareSchema, /PrepareAD, /Mode:Install) requires the
#                 server to be a domain member and an Enterprise/Schema Admin. The image has
#                 every Windows feature and prerequisite installed and the CU15 media staged
#                 in C:\TrueNorth\media\exchange2019.
#   sharepointse  The binaries need the site's product key (ADR 0012 decision 3: no keys in
#                 templates; TN_SHAREPOINT_PRODUCT_KEY at deploy) and the farm needs the
#                 domain. The image has the prerequisites installed, the media staged, and
#                 SQL Server 2022 as a SysPrep prepared image for the farm's own database.
#   mecm          ConfigMgr setup needs the domain, the AD schema extension (extadsch) and
#                 the System Management container. SQL Server 2022 is installed as a
#                 SysPrep "prepared image" (/ACTION=PrepareImage) and completed at deploy
#                 (/ACTION=CompleteImage); ADK + WinPE add-on are installed; the ConfigMgr
#                 media are staged.
# C:\TrueNorth\role-image.json in each image records what was installed and the deploy step
# still to run; the worker reads it as the staged-image marker.
#
# Build one with:  ./build.sh srv2022-exchange2019   (or packer build -only='*.vsphere-clone.<id>' .)

locals {
  role_images = {
    "srv2022-exchange2019" = {
      notes   = "TrueNorth role image: Server 2022 + Exchange 2019 CU15 prerequisites, media staged (setup at deploy)"
      isos    = ["${local.iso_dir}/${var.iso_exchange2019}"]
      cpus    = 4
      ram_mb  = 16384
      disk_mb = 204800
    }
    "srv2022-sharepointse" = {
      notes   = "TrueNorth role image: Server 2022 + SharePoint Subscription Edition prerequisites + SQL 2022 (prepared), media staged (setup at deploy)"
      isos    = ["${local.iso_dir}/${var.iso_sharepointse}", "${local.iso_dir}/${var.iso_sql2022}"]
      cpus    = 4
      ram_mb  = 16384
      disk_mb = 204800
    }
    "srv2022-sharepoint2019" = {
      notes = "TrueNorth role image: Server 2022 + SharePoint Server 2019 prerequisites + SQL 2022 (prepared), media staged (setup at deploy)"
      # SharePoint 2019 media, SQL Server 2022, and the Server 2022 ISO for .NET 3.5 (sources\sxs).
      isos    = ["${local.iso_dir}/${var.iso_sharepoint2019}", "${local.iso_dir}/${var.iso_sql2022}", "${local.iso_dir}/${var.iso_srv2022}"]
      cpus    = 4
      ram_mb  = 16384
      disk_mb = 204800
    }
    "srv2022-mecm" = {
      notes = "TrueNorth role image: Server 2022 + SQL 2022 (prepared) + ADK/WinPE + ConfigMgr media staged (setup at deploy)"
      # ConfigMgr media, SQL Server 2022, and the Server 2022 ISO for .NET 3.5 (sources\sxs).
      isos    = ["${local.iso_dir}/${var.iso_mecm}", "${local.iso_dir}/${var.iso_sql2022}", "${local.iso_dir}/${var.iso_srv2022}"]
      cpus    = 4
      ram_mb  = 16384
      disk_mb = 204800
    }
  }
}

source "vsphere-clone" "srv2022-exchange2019" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  template     = var.base_srv2022_template
  vm_name      = "srv2022-exchange2019"
  notes        = local.role_images["srv2022-exchange2019"].notes
  linked_clone = false
  network      = var.build_network
  CPUs         = local.role_images["srv2022-exchange2019"].cpus
  RAM          = local.role_images["srv2022-exchange2019"].ram_mb
  disk_size    = local.role_images["srv2022-exchange2019"].disk_mb

  cdrom_type   = "sata"
  iso_paths    = local.role_images["srv2022-exchange2019"].isos
  remove_cdrom = true

  customize {
    windows_options {
      computer_name         = "TN-ROLE-EXCH"
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

  shutdown_command = "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\sysprep.ps1"
  shutdown_timeout = "1h"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2022-exchange2019"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "srv2022-sharepointse" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  template     = var.base_srv2022_template
  vm_name      = "srv2022-sharepointse"
  notes        = local.role_images["srv2022-sharepointse"].notes
  linked_clone = false
  network      = var.build_network
  CPUs         = local.role_images["srv2022-sharepointse"].cpus
  RAM          = local.role_images["srv2022-sharepointse"].ram_mb
  disk_size    = local.role_images["srv2022-sharepointse"].disk_mb

  cdrom_type   = "sata"
  iso_paths    = local.role_images["srv2022-sharepointse"].isos
  remove_cdrom = true

  customize {
    windows_options {
      computer_name         = "TN-ROLE-SPSE"
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

  shutdown_command = "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\sysprep.ps1"
  shutdown_timeout = "1h"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2022-sharepointse"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "srv2022-sharepoint2019" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  template     = var.base_srv2022_template
  vm_name      = "srv2022-sharepoint2019"
  notes        = local.role_images["srv2022-sharepoint2019"].notes
  linked_clone = false
  network      = var.build_network
  CPUs         = local.role_images["srv2022-sharepoint2019"].cpus
  RAM          = local.role_images["srv2022-sharepoint2019"].ram_mb
  disk_size    = local.role_images["srv2022-sharepoint2019"].disk_mb

  cdrom_type   = "sata"
  iso_paths    = local.role_images["srv2022-sharepoint2019"].isos
  remove_cdrom = true

  customize {
    windows_options {
      computer_name         = "TN-ROLE-SP19"
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

  shutdown_command = "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\sysprep.ps1"
  shutdown_timeout = "1h"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2022-sharepoint2019"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "srv2022-mecm" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder

  template     = var.base_srv2022_template
  vm_name      = "srv2022-mecm"
  notes        = local.role_images["srv2022-mecm"].notes
  linked_clone = false
  network      = var.build_network
  CPUs         = local.role_images["srv2022-mecm"].cpus
  RAM          = local.role_images["srv2022-mecm"].ram_mb
  disk_size    = local.role_images["srv2022-mecm"].disk_mb

  cdrom_type   = "sata"
  iso_paths    = local.role_images["srv2022-mecm"].isos
  remove_cdrom = true

  customize {
    windows_options {
      computer_name         = "TN-ROLE-MECM"
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

  shutdown_command = "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Windows\\Temp\\sysprep.ps1"
  shutdown_timeout = "1h"

  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "srv2022-mecm"
      ovf     = true
      destroy = true
    }
  }
}

build {
  name = "windows-roles"
  sources = [
    "source.vsphere-clone.srv2022-exchange2019",
    "source.vsphere-clone.srv2022-sharepointse",
    "source.vsphere-clone.srv2022-sharepoint2019",
    "source.vsphere-clone.srv2022-mecm",
  ]

  provisioner "file" {
    source      = "${path.root}/files/windows/sysprep-unattend.xml"
    destination = "C:/Windows/Temp/sysprep-unattend.xml"
  }
  provisioner "file" {
    source      = "${path.root}/files/windows/sysprep.ps1"
    destination = "C:/Windows/Temp/sysprep.ps1"
  }

  # Pass 1: grow C:, Windows features, prerequisites from the depot.
  provisioner "powershell" {
    environment_vars = ["DEPOT_URL=${var.depot_url}", "DEPOT_USER=${var.depot_user}", "DEPOT_PASSWORD=${var.depot_password}", "TN_ROLE_IMAGE=${source.name}", "TN_ROLE_PASS=1"]
    script           = "${path.root}/files/windows/role-image.ps1"
  }
  provisioner "windows-restart" {
    restart_timeout = "30m"
  }
  # Pass 2: what needs the reboot first (SharePoint prerequisite installer, SQL
  # PrepareImage, ADK), then stage the media and write C:\TrueNorth\role-image.json.
  provisioner "powershell" {
    environment_vars = ["DEPOT_URL=${var.depot_url}", "DEPOT_USER=${var.depot_user}", "DEPOT_PASSWORD=${var.depot_password}", "TN_ROLE_IMAGE=${source.name}", "TN_ROLE_PASS=2"]
    script           = "${path.root}/files/windows/role-image.ps1"
  }
  # sysprep refuses to run with a reboot pending.
  provisioner "windows-restart" {
    restart_timeout = "30m"
  }
}
