# TrueNorth Range - Hyper-V Terraform Configuration
# Provisions differencing-disk VMs on a remote Hyper-V host via WinRM.
# Provider: https://registry.terraform.io/providers/taliesins/hyperv

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    hyperv = {
      source  = "taliesins/hyperv"
      version = "~> 1.0"
    }
  }

  # S3/MinIO backend for shared state (workspace-per-range isolation)
  # backend "s3" { ... }
}

provider "hyperv" {
  host     = var.hyperv_host
  user     = var.hyperv_user
  password = var.hyperv_password
  port     = var.hyperv_port
  https    = var.hyperv_https
  insecure = var.hyperv_insecure
  use_ntlm = true
  timeout  = "30s"
}

# ── Differencing VHDs ───────────────────────────────────────────────────────
# Each VM gets its own differencing VHDX backed by the golden image so that
# provisioning is fast and the base image is never modified.

resource "hyperv_vhd" "range_diff" {
  for_each = { for vm in var.vm_definitions : vm.name => vm }

  path        = "${var.vhd_path}\\${var.range_name}\\${each.value.name}.vhdx"
  parent_path = var.golden_image_path
  vhd_type    = "Differencing"
  size        = each.value.disk_gb * 1073741824  # bytes
}

# ── Virtual Machines ────────────────────────────────────────────────────────

resource "hyperv_machine_instance" "range_vm" {
  for_each = { for vm in var.vm_definitions : vm.name => vm }

  name               = "${var.range_name}-${each.value.name}"
  generation         = 2
  processor_count    = each.value.cores
  static_memory      = false
  dynamic_memory_min = 512
  dynamic_memory     = each.value.memory
  dynamic_memory_max = each.value.memory * 2
  notes              = "range_id=${var.range_id} role=${each.value.role}"

  vm_firmware {
    enable_secure_boot   = false
    secure_boot_template = "MicrosoftUEFICertificateAuthority"
    preferred_network_boot_protocol = "IPv4"
  }

  vm_processor {
    expose_virtualization_extensions = false
  }

  integration_services {
    "Guest Service Interface" = false
    "Heartbeat"               = true
    "Key-Value Pair Exchange"  = true
    "Shutdown"                = true
    "Time Synchronization"    = true
    "VSS"                     = true
  }

  hard_disk_drives {
    controller_type     = "Scsi"
    controller_number   = 0
    controller_location = 0
    path                = hyperv_vhd.range_diff[each.key].path
    disk_number         = 4294967295
  }

  network_adaptors {
    name              = "eth0"
    switch_name       = var.virtual_switch
    vlan_access       = each.value.vlan_tag > 0 ? true : false
    vlan_id           = each.value.vlan_tag
    dynamic_mac_address = true
  }

  depends_on = [hyperv_vhd.range_diff]
}

# ── Outputs ─────────────────────────────────────────────────────────────────

output "vms" {
  description = "Map of VM name → role (IP must be retrieved from DHCP/WinRM after boot)"
  value = {
    for k, vm in hyperv_machine_instance.range_vm :
    k => vm.name
  }
}

output "range_id" {
  value = var.range_id
}
