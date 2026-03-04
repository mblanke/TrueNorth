# TrueNorth Range - Proxmox Terraform Configuration
# Multi-node cluster support for large-scale cyber range deployments
# Designed for: 1,200 concurrent users, up to 70,000 VMs across cluster

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    proxmox = {
      source  = "telmate/proxmox"
      version = "~> 3.0"
    }
  }

  # S3/MinIO backend for shared state (workspace-per-range isolation)
  # Enable this block and configure for production:
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

provider "proxmox" {
  pm_api_url = var.pm_api_url
  pm_user    = var.pm_api_token_id != "" ? null : var.pm_user
  pm_password = var.pm_api_token_id != "" ? null : var.pm_password

  pm_api_token_id     = var.pm_api_token_id != "" ? var.pm_api_token_id : null
  pm_api_token_secret = var.pm_api_token_secret != "" ? var.pm_api_token_secret : null

  pm_tls_insecure = true
  pm_parallel     = 10  # parallel API calls per plan
  pm_timeout      = 600 # 10 min timeout for long clone ops
}

# --- Locals: Multi-Node Placement ---

locals {
  node_count = length(var.target_nodes)

  # Flatten VM definitions with computed node assignment
  vms = {
    for idx, vm in var.vm_definitions : vm.name => merge(vm, {
      index = idx
      # Round-robin placement across available nodes
      target_node = var.target_nodes[idx % local.node_count]
      # Unique VMID: range_offset + vm_index (prevents global collision)
      vmid = 100000 + idx
    })
  }

  # Common tags applied to all VMs in this range
  common_tags = "truenorth;range-${var.range_id};managed"
}

# --- VM Resources ---

resource "proxmox_vm_qemu" "range_vm" {
  for_each = local.vms

  name        = "${var.range_name}-${each.value.name}"
  target_node = each.value.target_node
  vmid        = each.value.vmid

  # Clone from template
  clone      = var.template_name
  full_clone = true
  os_type    = "cloud-init"

  # Resource allocation
  cores   = each.value.cores
  sockets = 1
  memory  = each.value.memory
  cpu     = "host"
  numa    = true
  hotplug = "network,disk,cpu,memory"

  # Enable QEMU guest agent
  agent = 1

  # Boot configuration
  boot     = "order=scsi0"
  onboot   = false # Ranges started on-demand
  oncreate = false # Don't auto-start after Terraform create

  # Disk configuration
  disks {
    scsi {
      scsi0 {
        disk {
          size    = each.value.disk_gb
          storage = var.storage_pool
          discard = true
          iothread = true
          ssd     = true
        }
      }
    }
  }

  # Network with VLAN isolation per range
  network {
    model   = "virtio"
    bridge  = "vmbr0"
    tag     = each.value.vlan_tag
    queues  = each.value.cores > 1 ? each.value.cores : 0
  }

  # Cloud-init configuration
  ipconfig0  = "ip=${each.value.ip},gw=${each.value.gateway}"
  ciuser     = var.cloud_init_user
  sshkeys    = var.cloud_init_ssh_keys

  # Tags for management
  tags = local.common_tags

  # Lifecycle
  lifecycle {
    ignore_changes = [
      network,
      target_node, # Don't force migrate on re-apply
    ]
  }

  timeouts {
    create = "10m"
    update = "5m"
    delete = "5m"
  }
}

# --- Outputs ---

output "range_id" {
  description = "Range identifier"
  value       = var.range_id
}

output "vm_inventory" {
  description = "Complete VM inventory with node placement"
  value = {
    for name, vm in proxmox_vm_qemu.range_vm : name => {
      vmid        = vm.vmid
      name        = vm.name
      target_node = vm.target_node
      cores       = vm.cores
      memory      = vm.memory
      ip          = vm.default_ipv4_address
      status      = vm.agent == 1 ? "agent-enabled" : "no-agent"
    }
  }
}

output "vm_ids" {
  description = "Map of VM names to VMIDs"
  value = {
    for name, vm in proxmox_vm_qemu.range_vm : name => vm.vmid
  }
}

output "vm_ips" {
  description = "Map of VM names to IP addresses"
  value = {
    for name, vm in proxmox_vm_qemu.range_vm : name => vm.default_ipv4_address
  }
}

output "node_distribution" {
  description = "Count of VMs placed on each Proxmox node"
  value = {
    for node in var.target_nodes : node => length([
      for name, vm in local.vms : name if vm.target_node == node
    ])
  }
}