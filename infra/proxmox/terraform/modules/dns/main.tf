# =============================================================================
# TrueNorth Range — DNS Module
# =============================================================================
# Manages per-range DNS zones with forward (A) and reverse (PTR) records
# using a local CoreDNS or dnsmasq instance.
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
  # Forward zone: e.g.  range-42.tn.local
  zone_name = "${var.range_domain}"

  # Reverse zone derived from the first record''s /24 network.
  # Assumes all VMs in the range share the same /24.
  first_ip       = length(var.vm_records) > 0 ? var.vm_records[0].ip : "0.0.0.0"
  octets         = split(".", local.first_ip)
  reverse_zone   = "${local.octets[2]}.${local.octets[1]}.${local.octets[0]}.in-addr.arpa"

  # CoreDNS zone file content — forward
  forward_records = join("\n", [
    for r in var.vm_records :
    "${r.name}.${local.zone_name}.  IN  A  ${r.ip}"
  ])

  # CoreDNS zone file content — reverse (PTR)
  ptr_records = join("\n", [
    for r in var.vm_records :
    "${element(split(".", r.ip), 3)}.${local.reverse_zone}.  IN  PTR  ${r.name}.${local.zone_name}."
  ])

  forward_zone_file = <<-EOZ
; TrueNorth Range DNS — ${local.zone_name}
; Auto-managed by Terraform — do not edit manually
$$ORIGIN ${local.zone_name}.
$$TTL 300

@   IN  SOA ns1.${local.zone_name}. admin.${local.zone_name}. (
        ${formatdate("YYYYMMDDhh", timestamp())}  ; serial
        3600       ; refresh
        600        ; retry
        86400      ; expire
        300 )      ; minimum TTL

@   IN  NS  ns1.${local.zone_name}.
ns1 IN  A   ${var.nameserver_ip}

${local.forward_records}
EOZ

  reverse_zone_file = <<-EOZ
; TrueNorth Range Reverse DNS — ${local.reverse_zone}
; Auto-managed by Terraform — do not edit manually
$$ORIGIN ${local.reverse_zone}.
$$TTL 300

@   IN  SOA ns1.${local.zone_name}. admin.${local.zone_name}. (
        ${formatdate("YYYYMMDDhh", timestamp())}  ; serial
        3600       ; refresh
        600        ; retry
        86400      ; expire
        300 )      ; minimum TTL

@   IN  NS  ns1.${local.zone_name}.

${local.ptr_records}
EOZ

  # CoreDNS Corefile snippet for this range
  corefile_snippet = <<-EOC
${local.zone_name} {
    file /etc/coredns/zones/db.${local.zone_name}
    log
    errors
}

${local.reverse_zone} {
    file /etc/coredns/zones/db.${local.reverse_zone}
    log
    errors
}
EOC
}

# ---------------------------------------------------------------------------
# Write forward zone file to DNS server
# ---------------------------------------------------------------------------

resource "null_resource" "forward_zone" {
  triggers = {
    records_hash = sha256(local.forward_zone_file)
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.nameserver_ip
      user        = var.dns_ssh_user
      private_key = var.dns_ssh_key
    }

    inline = [
      "mkdir -p /etc/coredns/zones",
      "cat > /etc/coredns/zones/db.${local.zone_name} <<'ZONEFILE'",
      local.forward_zone_file,
      "ZONEFILE",
    ]
  }
}

# ---------------------------------------------------------------------------
# Write reverse zone file to DNS server
# ---------------------------------------------------------------------------

resource "null_resource" "reverse_zone" {
  triggers = {
    records_hash = sha256(local.reverse_zone_file)
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.nameserver_ip
      user        = var.dns_ssh_user
      private_key = var.dns_ssh_key
    }

    inline = [
      "mkdir -p /etc/coredns/zones",
      "cat > /etc/coredns/zones/db.${local.reverse_zone} <<'ZONEFILE'",
      local.reverse_zone_file,
      "ZONEFILE",
    ]
  }
}

# ---------------------------------------------------------------------------
# Write/update CoreDNS Corefile snippet and reload
# ---------------------------------------------------------------------------

resource "null_resource" "corefile_update" {
  triggers = {
    zone_hash    = sha256(local.corefile_snippet)
    records_hash = sha256(join(",", [for r in var.vm_records : "${r.name}=${r.ip}"]))
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.nameserver_ip
      user        = var.dns_ssh_user
      private_key = var.dns_ssh_key
    }

    inline = [
      "mkdir -p /etc/coredns/zones.d",
      "cat > /etc/coredns/zones.d/${var.range_id}.conf <<'COREFILE'",
      local.corefile_snippet,
      "COREFILE",
      # Reassemble Corefile from snippets
      "echo '# Auto-generated Corefile' > /etc/coredns/Corefile",
      "echo '. { forward . ${var.upstream_dns} ; log ; errors }' >> /etc/coredns/Corefile",
      "for f in /etc/coredns/zones.d/*.conf; do cat \"$$f\" >> /etc/coredns/Corefile; done",
      "systemctl reload coredns || systemctl restart coredns",
    ]
  }

  depends_on = [
    null_resource.forward_zone,
    null_resource.reverse_zone,
  ]
}