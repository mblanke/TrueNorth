# TrueNorth Range — shared Windows build locals.
# pvscsi driver candidate paths on the mounted VMware Tools ISO (layout varies by Tools
# version, so WinPE is pointed at several letter+path combinations and uses the first hit).
locals {
  _tools_letters = ["D:", "E:", "F:", "G:"]
  _pvscsi_subpaths = [
    "\\Program Files\\VMware\\VMware Tools\\Drivers\\pvscsi\\Win8\\amd64",
    "\\Program Files\\VMware\\VMware Tools\\Drivers\\pvscsi\\Win10\\amd64",
    "\\Program Files\\VMware\\VMware Tools\\Drivers\\pvscsi\\Win2k22\\amd64",
  ]
  pvscsi_driver_paths = [
    for i, pair in setproduct(local._tools_letters, local._pvscsi_subpaths) :
    { key = format("%d", i + 1), path = "${pair[0]}${pair[1]}" }
  ]

  # ISO path builders.
  iso_dir = "[${var.iso_datastore}] ${var.iso_folder}"
}
