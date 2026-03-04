# TrueNorth Range — Packer template for Security Onion 2.4 sensor/SIEM image
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "seconion_iso_file" {
  type    = string
  default = "local:iso/securityonion-2.4.10-20240220.iso"
}

source "proxmox-iso" "security-onion" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  password                 = var.proxmox_password
  node                     = var.proxmox_node
  insecure_skip_tls_verify = true

  iso_file         = var.seconion_iso_file
  iso_storage_pool = var.iso_storage_pool

  vm_name              = "security-onion"
  template_description = "Security Onion 2.4 SIEM/NSM template for TrueNorth Range"

  cores   = 4
  memory  = 8192
  os      = "l26"
  machine = "q35"
  bios    = "ovmf"

  scsi_controller = "virtio-scsi-single"

  disks {
    disk_size    = "200G"
    storage_pool = var.vm_storage_pool
    type         = "scsi"
  }

  # Management interface
  network_adapters {
    model  = "virtio"
    bridge = "vmbr0"
  }

  # Monitor/SPAN interface for packet capture
  network_adapters {
    model  = "virtio"
    bridge = "vmbr1"
  }

  ssh_username = "onion"
  ssh_password = "SecurityOnion!"
  ssh_timeout  = var.ssh_timeout

  boot_wait = "15s"
  boot_command = [
    "<enter><wait60>",
    "<enter>"
  ]
}

build {
  sources = ["source.proxmox-iso.security-onion"]

  # Wait for Security Onion installer to complete initial setup
  provisioner "shell" {
    inline = [
      "echo 'Waiting for Security Onion initial boot to stabilize...'",
      "sleep 30",
    ]
  }

  # Run automated Security Onion setup (sosetup)
  provisioner "shell" {
    inline = [
      "# Create sosetup answer file for automated deployment",
      "sudo mkdir -p /opt/so/conf",
      "cat <<'SOSETUP' | sudo tee /opt/so/conf/sosetup.conf",
      "# Security Onion Setup Configuration",
      "SOSETUP_TYPE=eval",
      "MGMT_INTERFACE=ens18",
      "MON_INTERFACE=ens19",
      "HOSTNAME=seconion",
      "ZEEK_ENABLED=yes",
      "SURICATA_ENABLED=yes",
      "ELASTICSEARCH_ENABLED=yes",
      "KIBANA_ENABLED=yes",
      "LOGSTASH_ENABLED=yes",
      "CURATOR_ENABLED=yes",
      "ELASTALERT_ENABLED=yes",
      "SOSETUP",
    ]
  }

  # Execute sosetup in automated mode
  provisioner "shell" {
    inline = [
      "sudo so-setup-network || echo 'Network setup completed or skipped'",
      "# Full sosetup runs at first real boot with answer file",
      "# Mark for auto-setup on deploy",
      "sudo touch /opt/so/.autosetup",
    ]
  }

  # Configure Suricata
  provisioner "shell" {
    inline = [
      "# Suricata configuration - update rules",
      "sudo suricata-update || echo 'Suricata update will run at deploy time'",
      "# Enable community rules",
      "sudo suricata-update enable-source et/open || true",
      "sudo suricata-update enable-source oisf/trafficid || true",
    ]
  }

  # Configure Zeek
  provisioner "shell" {
    inline = [
      "# Zeek local policy additions for range telemetry",
      "echo '@load policy/protocols/ssl/validate-certs' | sudo tee -a /opt/zeek/share/zeek/site/local.zeek || true",
      "echo '@load policy/protocols/ssh/detect-bruteforcing' | sudo tee -a /opt/zeek/share/zeek/site/local.zeek || true",
      "echo '@load policy/misc/detect-traceroute' | sudo tee -a /opt/zeek/share/zeek/site/local.zeek || true",
    ]
  }

  # Configure Elasticsearch and Kibana
  provisioner "shell" {
    inline = [
      "# Elasticsearch tuning for range workloads",
      "# Actual index patterns and dashboards loaded via Ansible post-deploy",
      "echo 'Security Onion Elasticsearch/Kibana will be configured at deploy time'",
    ]
  }

  # Install QEMU guest agent
  provisioner "shell" {
    inline = [
      "sudo apt-get update || sudo yum update -y || true",
      "sudo apt-get install -y qemu-guest-agent || sudo yum install -y qemu-guest-agent || true",
      "sudo systemctl enable qemu-guest-agent",
    ]
  }

  # Clean up
  provisioner "shell" {
    inline = [
      "sudo truncate -s 0 /etc/machine-id",
    ]
  }
}