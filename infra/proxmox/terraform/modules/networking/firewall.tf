# =============================================================================
# TrueNorth Range — Firewall Rules (Proxmox VE Cluster Firewall)
# =============================================================================
# Policy: default DENY between ranges; allow only explicitly listed traffic.
# =============================================================================

# ---------------------------------------------------------------------------
# Security Group per range (acts as a reusable rule set)
# ---------------------------------------------------------------------------

resource "proxmox_virtual_environment_cluster_firewall_security_group" "range_sg" {
  name    = "range-${var.range_id}"
  comment = "Firewall rules for range ${var.range_id} (VLAN ${var.range_vlan_id})"

  # --- Rule 1: Default deny all inter-range traffic -----------------------
  rule {
    type    = "in"
    action  = "DROP"
    comment = "Default deny — block all ingress from other ranges"
    log     = "nolog"
    enable  = true
  }

  # --- Rule 2: Allow ICMP within the same range ---------------------------
  rule {
    type    = "in"
    action  = "ACCEPT"
    comment = "Allow ICMP within range ${var.range_id}"
    proto   = "icmp"
    source  = "+range-${var.range_id}-subnet"
    log     = "nolog"
    enable  = true
  }

  # --- Rule 3: Allow DNS (UDP/TCP 53) to management VLAN -----------------
  rule {
    type    = "out"
    action  = "ACCEPT"
    comment = "Allow DNS to management"
    proto   = "udp"
    dport   = "53"
    dest    = var.mgmt_subnet
    log     = "nolog"
    enable  = true
  }

  rule {
    type    = "out"
    action  = "ACCEPT"
    comment = "Allow DNS/TCP to management"
    proto   = "tcp"
    dport   = "53"
    dest    = var.mgmt_subnet
    log     = "nolog"
    enable  = true
  }

  # --- Rule 4: Allow NTP (UDP 123) to management VLAN --------------------
  rule {
    type    = "out"
    action  = "ACCEPT"
    comment = "Allow NTP to management"
    proto   = "udp"
    dport   = "123"
    dest    = var.mgmt_subnet
    log     = "nolog"
    enable  = true
  }

  # --- Rule 5: Allow monitoring agents → monitoring VLAN ------------------
  rule {
    type    = "out"
    action  = "ACCEPT"
    comment = "Allow monitoring agents to monitoring VLAN"
    proto   = "tcp"
    dport   = "9090,9100,3000,9200"
    dest    = var.monitoring_subnet
    log     = "nolog"
    enable  = true
  }

  # --- Rule 6: Allow established/related return traffic -------------------
  rule {
    type    = "in"
    action  = "ACCEPT"
    comment = "Allow established/related return traffic"
    macro   = "ACCEPT"
    log     = "nolog"
    enable  = true
  }
}

# ---------------------------------------------------------------------------
# Per-port allowlist rules (dynamic from var.allowed_ports)
# ---------------------------------------------------------------------------

resource "proxmox_virtual_environment_cluster_firewall_security_group" "range_port_allowlist" {
  count = var.internet_access && length(var.allowed_ports) > 0 ? 1 : 0

  name    = "range-${var.range_id}-ports"
  comment = "Allowed outbound ports for range ${var.range_id}"

  dynamic "rule" {
    for_each = var.allowed_ports
    content {
      type    = "out"
      action  = "ACCEPT"
      comment = "Allow TCP/${rule.value} to Internet"
      proto   = "tcp"
      dport   = tostring(rule.value)
      log     = "nolog"
      enable  = true
    }
  }
}

# ---------------------------------------------------------------------------
# Cluster-level default-deny policy between range VLANs
# ---------------------------------------------------------------------------

resource "proxmox_virtual_environment_firewall_rules" "inter_range_deny" {
  # Applied at the datacenter / cluster level

  rule {
    type    = "in"
    action  = "DROP"
    comment = "Block all inter-range traffic by default"
    source  = local.network
    enable  = true
    log     = "warning"
  }

  rule {
    type    = "out"
    action  = "DROP"
    comment = "Block all outbound inter-range traffic by default"
    dest    = local.network
    enable  = true
    log     = "warning"
  }

  depends_on = [
    proxmox_virtual_environment_cluster_firewall_security_group.range_sg,
  ]
}