# TrueNorth Range - vSphere Terraform Configuration
# Clones VMs from a template in a vCenter-managed cluster.
# Designed to match the provisioner_output schema used by VsphereAPIProvisioner
# and TerraformProvisioner(hypervisor_type="vsphere").

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    vsphere = {
      source  = "hashicorp/vsphere"
      version = "~> 2.8"
    }
  }

  # S3/MinIO backend for shared state (workspace-per-range isolation)
  # backend "s3" {
  #   bucket                      = "truenorth-tfstate"
  #   key                         = "ranges/terraform.tfstate"
  #   region                      = "us-east-1"
  #   endpoint                    = "http://minio:9000"
  #   access_key                  = "minio"
  #   secret_key                  = "minio123"
  #   skip_credentials_validation = true
  #   skip_metadata_api_check     = true
  #   skip_region_validation      = true
  #   force_path_style            = true
  #   workspace_key_prefix        = "ranges"
  # }
}

provider "vsphere" {
  vsphere_server       = var.vsphere_server
  user                 = var.vsphere_user
  password             = var.vsphere_password
  allow_unverified_ssl = var.vsphere_allow_unverified_ssl
}

# ── Data Sources ────────────────────────────────────────────────────────────

data "vsphere_datacenter" "dc" {
  name = var.datacenter
}

data "vsphere_compute_cluster" "cluster" {
  name          = var.cluster
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "ds" {
  name          = var.datastore
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "net" {
  name          = var.network
  datacenter_id = data.vsphere_datacenter.dc.id
}

# One data source per DISTINCT golden template referenced by the range,
# so a single range can be mixed-OS (clone each node from its own template).
data "vsphere_virtual_machine" "templates" {
  for_each      = toset([for vm in var.vm_definitions : vm.template_name])
  name          = each.value
  datacenter_id = data.vsphere_datacenter.dc.id
}

# ── VM Clones ───────────────────────────────────────────────────────────────

resource "vsphere_virtual_machine" "range_vm" {
  for_each = { for vm in var.vm_definitions : vm.name => vm }

  name             = "${var.range_name}-${each.value.name}"
  resource_pool_id = (
    var.resource_pool != ""
    ? data.vsphere_compute_cluster.cluster.resource_pool_id
    : data.vsphere_compute_cluster.cluster.resource_pool_id
  )
  datastore_id = data.vsphere_datastore.ds.id
  folder       = var.folder

  num_cpus = each.value.cores
  memory   = each.value.memory

  guest_id = data.vsphere_virtual_machine.templates[each.value.template_name].guest_id

  network_interface {
    network_id   = data.vsphere_network.net.id
    adapter_type = data.vsphere_virtual_machine.templates[each.value.template_name].network_interface_types[0]
  }

  disk {
    label            = "disk0"
    size             = each.value.disk_gb
    eagerly_scrub    = false
    thin_provisioned = data.vsphere_virtual_machine.templates[each.value.template_name].disks[0].thin_provisioned
  }

  clone {
    template_uuid = data.vsphere_virtual_machine.templates[each.value.template_name].id

    customize {
      # OS-aware guest customization so mixed Windows/Linux ranges customize correctly.
      dynamic "linux_options" {
        for_each = can(regex("(?i)win", each.value.os)) ? [] : [1]
        content {
          host_name = each.value.name
          domain    = "range.local"
        }
      }
      dynamic "windows_options" {
        for_each = can(regex("(?i)win", each.value.os)) ? [1] : []
        content {
          computer_name = substr(replace(each.value.name, "_", "-"), 0, 15)
        }
      }

      network_interface {
        ipv4_address = each.value.ip
        ipv4_netmask = each.value.netmask
      }

      ipv4_gateway    = each.value.gateway
      dns_server_list = var.dns_servers
    }
  }

  tags = [
    "range_id:${var.range_id}",
    "role:${each.value.role}",
  ]

  # Lifecycle guard: never destroy & recreate a VM that has already been
  # provisioned; the provisioner must explicitly call destroy first.
  lifecycle {
    ignore_changes = [tags]
  }
}

# ── Outputs ─────────────────────────────────────────────────────────────────

output "vms" {
  description = "Map of VM name → IP for use by the TrueNorth worker"
  value = {
    for k, vm in vsphere_virtual_machine.range_vm :
    k => vm.default_ip_address
  }
}

output "vm_ids" {
  description = "Map of VM name → vSphere Managed Object ID"
  value = {
    for k, vm in vsphere_virtual_machine.range_vm :
    k => vm.id
  }
}

output "range_id" {
  value = var.range_id
}
