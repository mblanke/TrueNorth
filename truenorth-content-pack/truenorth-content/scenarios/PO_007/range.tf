# PO 007 range — isolated enterprise segment, no egress. Instant-clone from Content Library.
# Crosswalk row: ALJQ / PO_007 / Analyze Malicious Activity in Network Traffic.
# Medium: pcap + IDS + NOC. Defensive — candidate analyses captured traffic; no live attack.
terraform { required_providers { vsphere = { source = "hashicorp/vsphere" } } }
variable "vlan_id" { default = 907 }

resource "vsphere_distributed_port_group" "range" {
  name    = "range-PO007-${var.vlan_id}"
  vlan_id = var.vlan_id      # isolated; NGFW/DFW policy = deny egress
}

# Golden templates consumed (all enabled=yes in vm_catalogue.csv):
#   srv2019        (DC/DNS + file server; traffic source/sink, pre-seeded)
#   precomp-host   (staged start-state: foothold with staged network artifacts)
#   win10-22h2     (victim/analyst workstations x2; one is the IDS/NOC analyst seat)
#   securityonion  (sensor + IDS -> OpenSearch; carries the pcap + alert feed the candidate reads)
#   kali           (NOC analyst pcap workstation; Wireshark/tshark/zeek for the deliverable)
#   usersim        (THE NOISE FLOOR: benign traffic generator -> plausible false positives)
#
# Each -> vsphere_virtual_machine { clone { ... instant } network_interface { range } }
# Instant-clone only; never full-clone. No egress from the range segment.