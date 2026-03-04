# =============================================================================
# TrueNorth Range — Networking Module
# =============================================================================
# Creates isolated network segments per range with VLAN tagging, NAT,
# DHCP, and DNS forwarding on a Proxmox VE cluster.
# =============================================================================

terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.46.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  # Derive a /24 gateway from the VLAN id:  10.<vlan_hi>.<vlan_lo>.1
  vlan_hi   = floor(var.range_vlan_id / 256)
  vlan_lo   = var.range_vlan_id % 256
  gateway   = "10.${local.vlan_hi}.${local.vlan_lo}.1"
  network   = "10.${local.vlan_hi}.${local.vlan_lo}.0/24"
  dhcp_start = "10.${local.vlan_hi}.${local.vlan_lo}.${var.dhcp_start_offset}"
  dhcp_end   = "10.${local.vlan_hi}.${local.vlan_lo}.${var.dhcp_end_offset}"

  bridge_name = "vmbr${var.range_vlan_id}"

  # Labels applied to every resource for traceability
  common_comment = "TrueNorth Range ${var.range_id} | VLAN ${var.range_vlan_id}"
}

# ---------------------------------------------------------------------------
# Linux Bridge (one per range VLAN on each target node)
# ---------------------------------------------------------------------------

resource "proxmox_virtual_environment_network_linux_bridge" "range_bridge" {
  for_each = toset(var.proxmox_nodes)

  node_name = each.value
  name      = local.bridge_name
  comment   = local.common_comment

  # Trunk the range VLAN over the physical uplink
  vlan_aware = true
  ports      = [var.uplink_interface]
}

# ---------------------------------------------------------------------------
# DHCP / DNS — dnsmasq instance via Proxmox SDN (or custom config)
# ---------------------------------------------------------------------------
# Proxmox SDN simple zones support built-in DHCP.  For bare-metal setups we
# provision a lightweight dnsmasq container; this section models the SDN path.

resource "proxmox_virtual_environment_network_linux_vlan" "range_vlan" {
  for_each = toset(var.proxmox_nodes)

  node_name = each.value
  name      = "vlan${var.range_vlan_id}"
  interface = local.bridge_name
  vlan      = var.range_vlan_id
  comment   = local.common_comment

  depends_on = [proxmox_virtual_environment_network_linux_bridge.range_bridge]
}

# ---------------------------------------------------------------------------
# NAT / Masquerade (only when internet access is enabled)
# ---------------------------------------------------------------------------
# We create a Proxmox-level firewall IPSet + rule that masquerades range
# traffic heading to the WAN.  When internet_access = false, no NAT rule is
# created and the default-deny policy blocks egress.

resource "proxmox_virtual_environment_firewall_ipset" "range_subnet" {
  name    = "range-${var.range_id}-subnet"
  comment = "Subnet for range ${var.range_id}"

  cidr {
    name    = local.network
    comment = "Range ${var.range_id} network"
  }
}

# ---------------------------------------------------------------------------
# DHCP scope definition (written as a Proxmox SDN subnet when SDN is used)
# ---------------------------------------------------------------------------

resource "null_resource" "dhcp_scope" {
  # Provision DHCP configuration on the gateway node via SSH.  Falls back to
  # a no-op when var.dhcp_enabled is false.
  count = var.dhcp_enabled ? 1 : 0

  triggers = {
    range_id = var.range_id
    vlan     = var.range_vlan_id
    gateway  = local.gateway
    start    = local.dhcp_start
    end      = local.dhcp_end
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.gateway_host
      user        = var.gateway_ssh_user
      private_key = var.gateway_ssh_key
    }

    inline = [
      "mkdir -p /etc/dnsmasq.d",
      "cat > /etc/dnsmasq.d/range-${var.range_id}.conf <<EOF",
      "# Auto-managed by TrueNorth Range Terraform — do not edit",
      "interface=${local.bridge_name}.${var.range_vlan_id}",
      "dhcp-range=${local.dhcp_start},${local.dhcp_end},255.255.255.0,12h",
      "dhcp-option=option:router,${local.gateway}",
      "dhcp-option=option:dns-server,${var.dns_server}",
      "server=${var.upstream_dns}",
      "EOF",
      "systemctl reload dnsmasq || systemctl restart dnsmasq",
    ]
  }
}

# ---------------------------------------------------------------------------
# NAT / Masquerade iptables rule on gateway (conditional)
# ---------------------------------------------------------------------------

resource "null_resource" "nat_masquerade" {
  count = var.internet_access ? 1 : 0

  triggers = {
    network = local.network
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.gateway_host
      user        = var.gateway_ssh_user
      private_key = var.gateway_ssh_key
    }

    inline = [
      "iptables -t nat -C POSTROUTING -s ${local.network} -o ${var.wan_interface} -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s ${local.network} -o ${var.wan_interface} -j MASQUERADE",
      "iptables -C FORWARD -i ${local.bridge_name}.${var.range_vlan_id} -o ${var.wan_interface} -j ACCEPT 2>/dev/null || iptables -A FORWARD -i ${local.bridge_name}.${var.range_vlan_id} -o ${var.wan_interface} -j ACCEPT",
      "iptables -C FORWARD -i ${var.wan_interface} -o ${local.bridge_name}.${var.range_vlan_id} -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -A FORWARD -i ${var.wan_interface} -o ${local.bridge_name}.${var.range_vlan_id} -m state --state RELATED,ESTABLISHED -j ACCEPT",
    ]
  }
}

# ---------------------------------------------------------------------------
# DNS forwarding rule on gateway
# ---------------------------------------------------------------------------

resource "null_resource" "dns_forwarding" {
  triggers = {
    range_id     = var.range_id
    upstream_dns = var.upstream_dns
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.gateway_host
      user        = var.gateway_ssh_user
      private_key = var.gateway_ssh_key
    }

    inline = [
      "iptables -t nat -C PREROUTING -i ${local.bridge_name}.${var.range_vlan_id} -p udp --dport 53 -j DNAT --to-destination ${var.dns_server}:53 2>/dev/null || iptables -t nat -A PREROUTING -i ${local.bridge_name}.${var.range_vlan_id} -p udp --dport 53 -j DNAT --to-destination ${var.dns_server}:53",
      "iptables -t nat -C PREROUTING -i ${local.bridge_name}.${var.range_vlan_id} -p tcp --dport 53 -j DNAT --to-destination ${var.dns_server}:53 2>/dev/null || iptables -t nat -A PREROUTING -i ${local.bridge_name}.${var.range_vlan_id} -p tcp --dport 53 -j DNAT --to-destination ${var.dns_server}:53",
    ]
  }
}