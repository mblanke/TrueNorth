# Variant: win11-dev — Windows 11 developer workstation.
# A realistic developer endpoint for supply-chain, credential-in-repo and secure-coding
# exercises.
#
# Metadata keys (variant_name .. variant_os_aliases) must each stay on ONE line:
# build.sh reads them to register the image.
variant_name        = "win11-dev"
variant_os_family   = "windows"
variant_base        = "win11-24h2"
variant_version     = "11 24H2"
variant_description = "Windows 11 developer workstation: VS Code, Git, Python, Node.js LTS, 7-Zip, Chrome"
variant_os_aliases  = ["windows-11-dev"]

variant_choco_packages = [
  "vscode",
  "git",
  "python",
  "nodejs-lts",
  "7zip",
  "googlechrome",
]
