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
  description = <<-EOT
    Windows Server 2019 media. Visual Studio ISO, latest refresh, file name like
    en-us_windows_server_2019_updated_<month>_<year>_x64_dvd_<id>.iso (srv2019_image_name +
    gvlk_srv2019), or the Evaluation ISO with srv2019_eval = true.
  EOT
  type        = string
  default     = "MUST-UPLOAD-FIRST_SERVER_2019_x64_en-us.iso"
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
  description = <<-EOT
    Windows 10 22H2 media. Visual Studio "business editions" ISO, file name like
    en-us_windows_10_business_editions_version_22h2_updated_<month>_<year>_x64_dvd_<id>.iso
    (win10_image_name + gvlk_win10), or the Evaluation ISO with win10_eval = true.
  EOT
  type        = string
  default     = "MUST-UPLOAD-FIRST_Win10_22H2_business_editions_x64.iso"
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

# Server-product media for the Windows role images (roles.pkr.hcl). Each is attached to a
# clone of srv2022 as an extra CD-ROM.
variable "iso_exchange2019" {
  description = "Exchange Server 2019 CU15 ISO (Visual Studio / Volume Licensing download)"
  type        = string
  default     = "MUST-UPLOAD-FIRST_ExchangeServer2019-x64-cu15.iso"
}
variable "iso_sharepointse" {
  description = "SharePoint Server Subscription Edition ISO"
  type        = string
  default     = "MUST-UPLOAD-FIRST_SharePoint_Server_Subscription_Edition_x64.iso"
}
variable "iso_sharepoint2019" {
  description = "SharePoint Server 2019 ISO (Visual Studio subscription)"
  type        = string
  default     = "en_sharepoint_server_2019_x64_dvd_68e34c9e.iso"
}
variable "iso_mecm" {
  description = "Microsoft Configuration Manager current branch baseline media (ISO with SMSSETUP\\)"
  type        = string
  default     = "MUST-UPLOAD-FIRST_ConfigMgr_current_branch_x64.iso"
}
variable "iso_sql2022" {
  description = "SQL Server 2022 media, used by srv2022-mecm (SQL is prepared, not configured, at build)"
  type        = string
  default     = "enu_sql_server_2022_enterprise_edition_x64_dvd_aa36de9e.iso"
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
    Base URL of TN-DEPOT01's Nexus repositories, e.g. http://10.30.31.30:8081/repository.
    Windows builds use <depot_url>/chocolatey (the NuGet v2 group feed): Chocolatey itself
    is bootstrapped from the chocolatey .nupkg in that feed (files/windows/choco-bootstrap.ps1)
    and every package comes from it. Role images and variant scripts fetch vendor files
    from <depot_url>/installers/... (the raw "installers" repository).
    Empty means "use the internet if reachable, otherwise skip baseline apps gracefully".
  EOT
  type        = string
  default     = ""
}

variable "depot_user" {
  description = <<-EOT
    Nexus user that may read the raw "installers" repository (tn-build-read, #214).
    Anonymous access to the depot is limited to the Chocolatey feed, so role images and
    the Office variant authenticate for <depot_url>/installers/... Empty: anonymous.
  EOT
  type        = string
  default     = ""
}

variable "depot_password" {
  description = <<-EOT
    Password of depot_user. Generated on TN-MGMT01 by build-phase.sh depot into
    /srv/truenorth/config/depot-build.env (root only); pkrvars-work.py --depot copies it
    into lab.auto.pkrvars.hcl (mode 0600). Never printed.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "apt_proxy" {
  description = <<-EOT
    HTTP forward proxy for apt and dnf during the build: TN-DEPOT01's apt-cacher-ng,
    e.g. http://10.30.31.30:3142. Applied in the Ubuntu autoinstall (apt: proxy), the
    Debian/Kali/Parrot preseeds (mirror/http/proxy), Rocky (dnf proxy=) and the derived
    and variant clones (files/linux/apt-proxy.sh). cleanup.sh removes it again, so no
    template ships with a build-network proxy; ranges pass TN_DEPOT_APT_PROXY at deploy.
    Empty means no proxy (offline builds).
  EOT
  type        = string
  default     = ""
  validation {
    condition     = var.apt_proxy == "" || can(regex("^http://[^/\\s]+/?$", var.apt_proxy))
    error_message = "The apt_proxy must be empty or a plain http://host:port URL (apt-cacher-ng refuses CONNECT tunnels)."
  }
}

variable "chocolatey_version" {
  description = "Chocolatey version to bootstrap from the depot feed; empty takes the newest stable one there"
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

# One generic Linux build login for every Linux template (ADR 0012 decision 5): every seed
# (ubuntu user-data, rocky ks.cfg, debian/kali/parrot preseed) is rendered with templatefile
# from these, and the derived images and variants log in with the same pair.
variable "ssh_username" {
  description = "Generic Linux build user, created by every Linux seed (sudo without a password)"
  type        = string
  default     = "ubuntu"
  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.ssh_username)) && !contains(["root"], var.ssh_username)
    error_message = "The ssh_username must be a lowercase POSIX user name (not root)."
  }
}

variable "ssh_password" {
  description = "Generic Linux build password: what Packer logs in with over SSH"
  type        = string
  sensitive   = true
  default     = "ubuntu"
}

variable "ssh_password_hash" {
  description = <<-EOT
    SHA-512 crypt ($6$) of ssh_password, written into every Linux seed. It must be the hash
    of ssh_password or the SSH login fails. scripts/lab/pkrvars-work.py computes it with
    `openssl passwd -6`; by hand: printf '%s\n' "$PW" | openssl passwd -6 -stdin.
    The default is the hash of the default password "ubuntu".
  EOT
  type        = string
  sensitive   = true
  default     = "$6$truenorthpkr$bVdgsetfx5CL7JMrDQFcHUqCOak0SE9VomTEvLepAaIO.OLaaIqggPw9zrNQ1AuWF73ChZfbTQKJOnkAIKymj0"
  validation {
    condition     = can(regex("^\\$6\\$(rounds=[0-9]+\\$)?[./A-Za-z0-9]{1,16}\\$[./A-Za-z0-9]{86}$", var.ssh_password_hash))
    error_message = "The ssh_password_hash must be a SHA-512 crypt string ($6$salt$hash), e.g. from openssl passwd -6."
  }
}

# ----------------------------------------------------------------------------
# Windows edition-select keys (public GVLK / generic keys — NOT activation, NOT secret)
# These only tell Setup which edition of the install.wim to lay down so the install
# proceeds unattended. Eval media ignore them; retail/VL media require one.
# GVLKs: Microsoft Learn, "Key Management Services (KMS) client activation and product
# keys", https://learn.microsoft.com/windows-server/get-started/kms-client-activation-keys
# (checked 2026-10-19 against the page dated 2025-06-25).
# ----------------------------------------------------------------------------
variable "win10_eval" {
  description = "true when iso_win10 is the Evaluation ISO: image \"Windows 10 Enterprise Evaluation\", no key"
  type        = bool
  default     = false
}
variable "win10_image_name" {
  description = <<-EOT
    install.wim image name on the Windows 10 22H2 business-editions ISO (ignored when
    win10_eval). Check with `dism /Get-WimInfo /WimFile:<iso>\sources\install.wim`.
  EOT
  type        = string
  default     = "Windows 10 Enterprise"
}
variable "gvlk_win10" {
  description = "Windows 10 Enterprise GVLK (public KMS client setup key; ignored when win10_eval)"
  type        = string
  default     = "NPPR9-FWDCX-D2C8J-H872K-2YT43"
}
variable "srv2019_eval" {
  description = "true when iso_srv2019 is the Evaluation ISO: Standard Evaluation image, no key"
  type        = bool
  default     = false
}
variable "srv2019_image_name" {
  description = <<-EOT
    install.wim image name on the Server 2019 ISO (ignored when srv2019_eval). Some media
    name it "Windows Server 2019 SERVERSTANDARD"; check with dism /Get-WimInfo.
  EOT
  type        = string
  default     = "Windows Server 2019 Standard (Desktop Experience)"
}
variable "gvlk_srv2019" {
  description = "Server 2019 Standard GVLK (public KMS client setup key; ignored when srv2019_eval)"
  type        = string
  default     = "N69G4-B89J2-4G8F4-WWYCC-J464C"
}
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
variable "base_srv2022_template" {
  description = "Inventory template name the Windows Server role images (roles.pkr.hcl) clone from"
  type        = string
  default     = "srv2022"
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
