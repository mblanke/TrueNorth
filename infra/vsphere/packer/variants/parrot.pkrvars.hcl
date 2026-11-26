# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# Variant: parrot — Parrot Security 7 (echo), built from the debian13 golden template.
# The Parrot 7.3 ISO has no debian-installer (a live system with the Calamares installer
# only), so it cannot be installed unattended. Parrot 7 is Debian 13 based: this converts a
# clean debian13 clone to Parrot from Parrot's signed repository (through the depot's apt
# proxy, Remap-tnparrot) and boots to the MATE desktop.
#
# Metadata keys (variant_name .. variant_os_aliases) must each stay on ONE line:
# build.sh reads them to register the image.
variant_name        = "parrot"
variant_os_family   = "linux"
variant_base        = "debian13"
variant_version     = "7 (echo)"
variant_description = "Parrot Security 7 (echo) with the MATE desktop and parrot-tools-full, converted from debian13"
variant_os_aliases  = ["parrot", "parrot-security"]

variant_apt_packages = []

variant_scripts = ["parrot-from-debian.sh"]
variant_cpus    = 2
variant_ram_mb  = 4096
variant_disk_mb = 81920
