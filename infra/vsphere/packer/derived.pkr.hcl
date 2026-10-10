# TrueNorth Range — derived golden templates (vsphere-clone).
# These clone an existing INVENTORY template (not a Content Library item — vsphere-clone's
# `template` must resolve in vCenter inventory) and provision a role on top. That is why
# every base build keeps convert_to_template=true: it leaves an inventory template for
# these to source from, while still publishing OVF to the Content Library for the deploy
# provisioner. Build one with:  packer build -only='*.vsphere-clone.<id>' .
# (or ./build.sh <id>). A bare 'vsphere-clone.<id>' matches nothing: the builds are named.
#
# Linux-derived clone var.base_ubuntu_template over SSH (the ubuntu build user survives the
# template). Windows-derived clone var.base_win10_template but the base is sysprep-generalized,
# so there is no build-time WinRM path; they clone + convert only (communicator="none") and
# their role script is applied at deploy via a guest-customization spec. See README.

locals {
  linux_derived = {
    remnux           = "REMnux malware-analysis workstation"
    sift             = "SANS SIFT DFIR workstation"
    "svc-emulators"  = "DNS/NTP/mail/web service emulation"
    "greyspace-host" = "Greyspace gs-core: Docker + every stack image pre-loaded (ADR 0007)"
    "ca-host"        = "step-ca certificate authority"
    usersim          = "GHOSTS noise-floor user simulation"
    "cloudlog-emu"   = "Azure/AWS cloud log emulation"
    "c2-server"      = "open-source C2 (Sliver/Mythic) — SIGN-OFF REQUIRED (catalogue enabled=no)"
  }
}

# ---- Linux-derived (clone ubuntu-lts, provision role over SSH) -----------------------
source "vsphere-clone" "remnux" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "remnux"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "remnux"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "sift" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "sift"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "sift"
      ovf     = true
      destroy = true
    }
  }
}

# Greyspace host (ADR 0007): the image the worker clones a range's gs-core VM from
# (GREYSPACE_HOST_TEMPLATE, default greyspace-host). Docker, NFS client, open-vm-tools,
# and every Greyspace stack image pre-loaded (ranges have no internet to pull from).
source "vsphere-clone" "greyspace-host" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "greyspace-host"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "greyspace-host"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "svc-emulators" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "svc-emulators"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "svc-emulators"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "ca-host" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "ca-host"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "ca-host"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "usersim" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "usersim"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "usersim"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "cloudlog-emu" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "cloudlog-emu"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "cloudlog-emu"
      ovf     = true
      destroy = true
    }
  }
}

# c2-server: catalogue enabled=no. Included but requires instructor/Standards sign-off.
source "vsphere-clone" "c2-server" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_ubuntu_template
  vm_name             = "c2-server"
  linked_clone        = false
  communicator        = "ssh"
  ssh_username        = var.ssh_username
  ssh_password        = var.ssh_password
  ssh_timeout         = "30m"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "c2-server"
      ovf     = true
      destroy = true
    }
  }
}

build {
  name = "linux-derived"
  sources = [
    "source.vsphere-clone.remnux",
    "source.vsphere-clone.sift",
    "source.vsphere-clone.svc-emulators",
    "source.vsphere-clone.greyspace-host",
    "source.vsphere-clone.ca-host",
    "source.vsphere-clone.usersim",
    "source.vsphere-clone.cloudlog-emu",
    "source.vsphere-clone.c2-server",
  ]
  provisioner "shell" {
    only   = ["vsphere-clone.remnux"]
    script = "${path.root}/files/linux/roles/remnux.sh"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.sift"]
    script = "${path.root}/files/linux/roles/sift.sh"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.svc-emulators"]
    script = "${path.root}/files/linux/roles/svc-emulators.sh"
  }
  provisioner "file" {
    only        = ["vsphere-clone.greyspace-host"]
    source      = "${path.root}/../../../greyspace/images"
    destination = "/tmp/greyspace-images"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.greyspace-host"]
    script = "${path.root}/files/linux/roles/greyspace-host.sh"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.ca-host"]
    script = "${path.root}/files/linux/roles/ca-host.sh"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.usersim"]
    script = "${path.root}/files/linux/roles/usersim.sh"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.cloudlog-emu"]
    script = "${path.root}/files/linux/roles/cloudlog-emu.sh"
  }
  provisioner "shell" {
    only   = ["vsphere-clone.c2-server"]
    script = "${path.root}/files/linux/roles/c2-server.sh"
  }
  provisioner "shell" {
    script = "${path.root}/files/linux/cleanup.sh"
  }
}

# ---- Windows-derived (clone win10-22h2; role applied at deploy, see README) ----------
# The base is sysprep-generalized, so there is no build-time WinRM path. These clone and
# template only; precomp-host.ps1 / detonation-host.ps1 are staged for deploy-time use.
source "vsphere-clone" "precomp-host" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_win10_template
  vm_name             = "precomp-host"
  linked_clone        = false
  communicator        = "none"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "precomp-host"
      ovf     = true
      destroy = true
    }
  }
}

source "vsphere-clone" "detonation-host" {
  vcenter_server      = var.vcenter_server
  username            = var.vcenter_username
  password            = var.vcenter_password
  insecure_connection = var.insecure_connection
  datacenter          = var.vsphere_datacenter
  cluster             = var.vsphere_cluster
  host                = var.vsphere_host
  datastore           = var.vsphere_datastore
  folder              = var.vsphere_folder
  template            = var.base_win10_template
  vm_name             = "detonation-host"
  linked_clone        = false
  communicator        = "none"
  convert_to_template = true
  dynamic "content_library_destination" {
    for_each = var.publish_to_library ? [1] : []
    content {
      library = var.content_library
      name    = "detonation-host"
      ovf     = true
      destroy = true
    }
  }
}

build {
  name = "windows-derived"
  sources = [
    "source.vsphere-clone.precomp-host",
    "source.vsphere-clone.detonation-host",
  ]
  # communicator="none": no build-time provisioning. Role scripts (files/windows/
  # precomp-host.ps1, detonation-host.ps1) are applied at deploy via guest customization.
}
