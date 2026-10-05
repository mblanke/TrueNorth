# TrueNorth Range — vSphere Packer shared variables.
#
# Every *.pkr.hcl in this directory is loaded together by Packer, so each variable
# is declared exactly once here and referenced by every source. Override real values
# in lab.auto.pkrvars.hcl (git-ignored); see lab.auto.pkrvars.hcl.example.

# ----------------------------------------------------------------------------
# vCenter / placement
# ----------------------------------------------------------------------------
variable "vcenter_server" {
  description = "vCenter FQDN or IP (lab: vcenter.truenorth.lab / 192.168.1.10)"
  type        = string
  default     = "vcenter.truenorth.lab"
}

variable "vcenter_username" {
  description = "vCenter SSO user"
  type        = string
  default     = "administrator@vsphere.local"
}

variable "vcenter_password" {
  description = "vCenter SSO password"
  type        = string
  sensitive   = true
  default     = ""
}

variable "insecure_connection" {
  description = "Skip vCenter TLS validation (true for the lab's self-signed cert)"
  type        = bool
  default     = true
}

variable "vsphere_datacenter" {
  description = "Target datacenter (lab: DC-Lab)"
  type        = string
  default     = "DC-Lab"
}

variable "vsphere_cluster" {
  description = "Target compute cluster. Leave empty to build on vsphere_host directly."
  type        = string
  default     = ""
}

variable "vsphere_host" {
  description = <<-EOT
    ESXi build host. MUST be set to the host that owns the ISO datastore, because the
    ISOs live on a host-local datastore ([esx01-local] ISO/). Lab: esx01.truenorth.lab
    (or 192.168.1.11). Empty means "let the cluster pick a host" — only valid when the
    ISO datastore is shared, which it is NOT in this lab.
  EOT
  type        = string
  default     = ""
}

variable "vsphere_datastore" {
  description = "Datastore the built VM/template is written to (lab: esx01-local)"
  type        = string
  default     = "esx01-local"
}

variable "vsphere_folder" {
  description = "VM/template inventory folder"
  type        = string
  default     = "TrueNorth/Templates"
}

# ----------------------------------------------------------------------------
# ISO location — ISOs are already staged on a host-local datastore
# ----------------------------------------------------------------------------
variable "iso_datastore" {
  description = "Datastore holding the pre-staged install ISOs (lab: esx01-local)"
  type        = string
  default     = "esx01-local"
}

variable "iso_folder" {
  description = "Folder on iso_datastore holding the ISOs"
  type        = string
  default     = "ISO"
}

# One variable per ISO filename. Exact names come from the datastore ISO folder.
# "must upload first" placeholders mark media NOT present in the lab today.
variable "iso_srv2022" {
  type    = string
  default = "en-us_windows_server_2022_updated_july_2026_x64_dvd_75aa9e18.iso"
}
variable "iso_srv2025" {
  type    = string
  default = "en-us_windows_server_2025_updated_july_2026_x64_dvd_4e6f5a42.iso"
}
variable "iso_srv2019" {
  type    = string
  default = "MUST-UPLOAD-FIRST_SERVER_2019_EVAL_x64FRE_en-us.iso"
}
variable "iso_srv2016" {
  type    = string
  default = "MUST-UPLOAD-FIRST_SERVER_2016_EVAL_x64FRE_en-us.iso"
}
variable "iso_win11_24h2" {
  type    = string
  default = "en-us_windows_11_consumer_editions_version_24h2_updated_feb_2026_x64_dvd_4e400c9e.iso"
}
variable "iso_win11_26h1" {
  type    = string
  default = "en-us_windows_11_consumer_editions_version_26h1_updated_july_2026_x64_dvd_f69a9a1e.iso"
}
variable "iso_win10" {
  type    = string
  default = "MUST-UPLOAD-FIRST_Win10_Enterprise_Eval_x64.iso"
}
variable "iso_win10_ltsc" {
  description = "Windows 10 Enterprise LTSC 2021 media (eval or volume-licence)"
  type        = string
  default     = "MUST-UPLOAD-FIRST_Win10_Enterprise_LTSC_2021_x64.iso"
}
variable "iso_ubuntu" {
  type    = string
  default = "ubuntu-24.04.4-live-server-amd64.iso"
}
variable "iso_kali" {
  type    = string
  default = "kali-linux-2026.1-installer-everything-amd64.iso"
}
variable "iso_rocky" {
  type    = string
  default = "Rocky-10.2-x86_64-dvd1.iso"
}
variable "iso_debian" {
  type    = string
  default = "debian-13.6.0-amd64-DVD-1.iso"
}
variable "iso_parrot" {
  type    = string
  default = "Parrot-security-7.3_amd64.iso"
}
variable "iso_pfsense" {
  type    = string
  default = "MUST-UPLOAD-FIRST_pfSense-CE-2.7.2-RELEASE-amd64.iso"
}
variable "iso_securityonion" {
  type    = string
  default = "MUST-UPLOAD-FIRST_securityonion-2.4.10-20240220.iso"
}
variable "iso_vyos" {
  type    = string
  default = "MUST-UPLOAD-FIRST_vyos-1.4-generic-amd64.iso"
}

variable "vmtools_iso_path" {
  description = <<-EOT
    Full datastore path to the VMware Tools (windows.iso) used by the Windows builds to
    supply the pvscsi driver during WinPE. ESXi ships it at the path below. If your hosts
    do not have the Tools depot, upload windows.iso to the ISO folder and point here.
  EOT
  type        = string
  default     = "[] /vmimages/tools-isoimages/windows.iso"
}

# ----------------------------------------------------------------------------
# Networking
# ----------------------------------------------------------------------------
variable "build_network" {
  description = <<-EOT
    Port group the build VM's primary NIC attaches to. Needs DHCP and must be able to
    reach the Packer host (this machine) for HTTP-served seeds (kali, pfsense) and for
    WinRM/SSH callbacks. Lab: the build host is TN-BUILD01 on dPG-TN-BUILD, the only
    network with internet egress (vm-build-sheet.md §1/§8). Never build on dPG-TN-MGMT.
  EOT
  type        = string
  default     = "dPG-TN-BUILD"
}

variable "pfsense_lan_network" {
  description = "Port group for the pfSense LAN NIC (defaults to build_network)"
  type        = string
  default     = ""
}

variable "http_bind_address" {
  description = "Address Packer's HTTP server binds to; empty lets Packer choose the reachable one"
  type        = string
  default     = ""
}
variable "http_port_min" {
  type    = number
  default = 8800
}
variable "http_port_max" {
  type    = number
  default = 8899
}

# ----------------------------------------------------------------------------
# Output: inventory template and/or Content Library
# ----------------------------------------------------------------------------
variable "content_library" {
  description = "vCenter Content Library name to publish OVF templates into (lab: TrueNorth-Templates)"
  type        = string
  default     = "TrueNorth-Templates"
}

variable "publish_to_library" {
  description = <<-EOT
    When true, each base build also publishes an OVF item named after its catalogue id
    into content_library. Inventory templates (convert_to_template=true) are ALWAYS kept
    so the vsphere-clone derived builds have an inventory source to clone from.
  EOT
  type        = bool
  default     = true
}

# ----------------------------------------------------------------------------
# Software depot (no-egress ranges reach this; builds may use it when set)
# ----------------------------------------------------------------------------
variable "depot_url" {
  description = <<-EOT
    Base URL of the internal software depot (TN-DEPOT01). When set, Windows builds point
    Chocolatey at the depot /chocolatey feed and Linux builds may use an apt mirror under it.
    Empty means "use the internet if reachable, otherwise skip baseline apps gracefully".
  EOT
  type        = string
  default     = ""
}

# ----------------------------------------------------------------------------
# Build credentials (build-time only)
# ----------------------------------------------------------------------------
variable "windows_admin_password" {
  description = <<-EOT
    Local Administrator password used DURING the build only. Sysprep generalize resets
    the machine SID/OOBE; range deploy sets the real credentials. Never a production
    secret — override per site.
  EOT
  type        = string
  sensitive   = true
  default     = "TN-Build-Pass1!"
}

variable "ssh_username" {
  description = "Build SSH user baked into the Linux seeds"
  type        = string
  default     = "ubuntu"
}

variable "ssh_password" {
  description = "Build SSH password baked into the Linux seeds (hash lives in the seed files)"
  type        = string
  sensitive   = true
  default     = "ubuntu"
}

# ----------------------------------------------------------------------------
# Windows edition-select keys (public GVLK / generic keys — NOT activation, NOT secret)
# These only tell Setup which edition of the install.wim to lay down so the install
# proceeds unattended. Eval media ignore them; retail/VL media require one.
# ----------------------------------------------------------------------------
variable "gvlk_srv2022" {
  description = "Server 2022 Standard GVLK (public KMS client setup key)"
  type        = string
  default     = "VDYBN-27WPP-V4HQT-9VMD4-VMK7H"
}
variable "gvlk_srv2025" {
  description = "Server 2025 Standard GVLK (public KMS client setup key)"
  type        = string
  default     = "TVRH6-WHNXV-R9WG3-9XRFY-MY832"
}
variable "gvlk_win11_pro" {
  description = "Windows 11 Pro generic edition-select key (public)"
  type        = string
  default     = "VK7JG-NPHTM-C97JM-9MPGT-3V66T"
}
variable "gvlk_win10_ltsc" {
  description = <<-EOT
    Windows 10 Enterprise LTSC 2021 GVLK (public KMS client setup key). Volume-licence
    media need it to install unattended. Set it to "" when iso_win10_ltsc is the
    Evaluation ISO, which takes no key.
  EOT
  type        = string
  default     = "M7XTQ-FN8P6-TTKYV-9D4CC-J462D"
}
variable "win10_ltsc_image_name" {
  description = <<-EOT
    install.wim image name on the LTSC ISO. Volume-licence media: "Windows 10 Enterprise
    LTSC 2021". Evaluation media: "Windows 10 Enterprise LTSC Evaluation". Check with
    `dism /Get-WimInfo /WimFile:<iso>\sources\install.wim` if Setup stops at image select.
  EOT
  type        = string
  default     = "Windows 10 Enterprise LTSC 2021"
}

variable "win11_vtpm" {
  description = <<-EOT
    When true, add a vTPM device to the Win11 build (requires a vCenter key provider) and
    skip the LabConfig TPM/SecureBoot/RAM bypass. When false (default), no vTPM and the
    bypasses are injected in WinPE so Win11 installs on a plain VM.
  EOT
  type        = bool
  default     = false
}

# ----------------------------------------------------------------------------
# Derived (vsphere-clone) base templates — inventory template names to clone from
# ----------------------------------------------------------------------------
variable "base_ubuntu_template" {
  description = "Inventory template name the Linux derived images clone from"
  type        = string
  default     = "ubuntu-lts"
}
variable "base_win10_template" {
  description = "Inventory template name the Windows derived images clone from"
  type        = string
  default     = "win10-22h2"
}

# ----------------------------------------------------------------------------
# Custom image variants (variants.pkr.hcl). One variants/<name>.pkrvars.hcl per variant
# sets these; build.sh passes it with -var-file. Every one has a default so the rest of
# the directory validates and builds without a variant file.
# ----------------------------------------------------------------------------
variable "variant_name" {
  description = "Variant id: VM/template name, Content Library item and golden-image catalogue_id"
  type        = string
  default     = "variant-unset"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.variant_name))
    error_message = "The variant_name must be lowercase letters, digits and hyphens (2-63 chars)."
  }
}
variable "variant_base" {
  description = <<-EOT
    Inventory template the variant clones (a base built by this directory, e.g. win11-24h2).
    The default is a placeholder that keeps `packer validate .` green without a variant
    file; a variant file always sets the real one.
  EOT
  type        = string
  default     = "variant-base-unset"
}
variable "variant_os_family" {
  description = "windows or linux; picks the variant source and is registered as os_family"
  type        = string
  default     = "linux"
  validation {
    condition     = contains(["windows", "linux"], var.variant_os_family)
    error_message = "The variant_os_family must be \"windows\" or \"linux\"."
  }
}
variable "variant_version" {
  description = "Human version string registered with the image (e.g. \"11 24H2\")"
  type        = string
  default     = ""
}
variable "variant_description" {
  description = "One line describing the variant; registered as the image role and set as VM notes"
  type        = string
  default     = ""
}
variable "variant_os_aliases" {
  description = "Extra topology OS aliases that should resolve to this variant (its name always does)"
  type        = list(string)
  default     = []
}
variable "variant_choco_packages" {
  description = <<-EOT
    Chocolatey package ids for a Windows variant. Pin a version with "id@1.2.3".
    Installed from the depot feed when depot_url is set, else the community feed.
  EOT
  type        = list(string)
  default     = []
}
variable "variant_apt_packages" {
  description = "Distro packages for a Linux variant (apt on Debian/Ubuntu bases, dnf on Rocky)"
  type        = list(string)
  default     = []
}
variable "variant_scripts" {
  description = <<-EOT
    Extra scripts, relative to variants/scripts/, run after the packages in the order given.
    .ps1 for Windows variants, .sh for Linux variants.
  EOT
  type        = list(string)
  default     = []
}
variable "variant_cpus" {
  description = "vCPUs for the variant; 0 keeps the base template's"
  type        = number
  default     = 0
}
variable "variant_ram_mb" {
  description = "RAM (MB) for the variant; 0 keeps the base template's"
  type        = number
  default     = 0
}
variable "variant_disk_mb" {
  description = "Grow the system disk to this size (MB); 0 keeps the base template's. Never shrinks."
  type        = number
  default     = 0
}

# Convenience: evaluates to the cluster when set, else the single host.
locals {
  pfsense_lan = var.pfsense_lan_network != "" ? var.pfsense_lan_network : var.build_network
}
