# PO 009 range — isolated enterprise segment, no egress. Instant-clone from Content Library.
terraform { required_providers { vsphere = { source = "hashicorp/vsphere" } } }
variable "vlan_id" { default = 909 }

resource "vsphere_distributed_port_group" "range" {
  name    = "range-PO009-${var.vlan_id}"
  vlan_id = var.vlan_id      # isolated; NGFW/DFW policy = deny egress
}
# Golden templates consumed (all enabled in vm_catalogue.csv):
#   srv2019 (DC + file server, pre-seeded)   precomp-host (staged start-state)
#   win10-22h2 x2 (workstations)             securityonion (sensor/telemetry -> OpenSearch)
#   cloudlog-emu (Azure/AWS log set for EO 009.03)   usersim (noise floor)
# Each -> vsphere_virtual_machine { clone { ... instant } network_interface { range } }
