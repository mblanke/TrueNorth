# Variant: win10-office — Windows 10 office-worker workstation (build sheet §5.2).
# Everyday user apps for phishing, document-borne and user-activity scenarios.
# Microsoft Office is not on the community Chocolatey feed; office-ltsc-from-depot.ps1
# installs Office LTSC from TN-DEPOT01 when the depot carries it, and otherwise logs a
# warning and leaves Office out.
#
# Metadata keys (variant_name .. variant_os_aliases) must each stay on ONE line:
# build.sh reads them to register the image.
variant_name        = "win10-office"
variant_os_family   = "windows"
variant_base        = "win10-22h2"
variant_version     = "10 22H2"
variant_description = "Windows 10 office workstation: Office LTSC (from depot), PDF reader, browsers, media"
variant_os_aliases  = ["windows-10-office"]

variant_choco_packages = [
  "adobereader",
  "googlechrome",
  "firefoxesr",
  "7zip",
  "vlc",
  "notepadplusplus",
]

variant_scripts = ["office-ltsc-from-depot.ps1"]

variant_disk_mb = 61440
