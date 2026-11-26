# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# Variant: kali-gui — Kali analyst/attacker workstation with the standard Kali XFCE desktop.
# Cloned from the kali golden template (command line only); boots to the LightDM login.
#
# Metadata keys (variant_name .. variant_os_aliases) must each stay on ONE line:
# build.sh reads them to register the image.
variant_name        = "kali-gui"
variant_os_family   = "linux"
variant_base        = "kali"
variant_version     = "2026.1"
variant_description = "Kali 2026.1 workstation with the Kali XFCE desktop (LightDM), for browser-console use"
variant_os_aliases  = ["kali-gui", "kali-desktop"]

variant_apt_packages = [
  "kali-desktop-xfce",
  "open-vm-tools-desktop",
]

variant_scripts = ["kali-gui-desktop.sh"]
variant_cpus    = 2
variant_ram_mb  = 4096
variant_disk_mb = 40960
