# Variant: win11-analyst — Windows 11 blue-team analyst workstation (build sheet §5.3).
# Packet, host-forensics and reverse-engineering tools for SOC / DFIR exercises.
# Wireshark: the Chocolatey package does not install Npcap (its free licence forbids a
# silent install), so this image reads and dissects pcaps but cannot live-capture. Add
# Npcap OEM from the depot through a variant script if live capture is needed.
#
# Metadata keys (variant_name .. variant_os_aliases) must each stay on ONE line:
# build.sh reads them to register the image.
variant_name        = "win11-analyst"
variant_os_family   = "windows"
variant_base        = "win11-24h2"
variant_version     = "11 24H2"
variant_description = "Windows 11 analyst workstation: Wireshark, Sysinternals, NetworkMiner, Autopsy, Ghidra, VS Code, Git, Python"
variant_os_aliases  = ["windows-11-analyst"]

variant_choco_packages = [
  "wireshark",
  "sysinternals",
  "networkminer",
  "autopsy",
  "ghidra",
  "vscode",
  "git",
  "python",
]

# Autopsy and Ghidra are memory-hungry; give the analyst image room to work.
variant_cpus    = 4
variant_ram_mb  = 8192
variant_disk_mb = 81920
