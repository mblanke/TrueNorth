# TrueNorth Range — vSphere Packer plugin requirements (shared).
# One packer{} block for the whole directory; every .pkr.hcl here is loaded together,
# so the plugin requirement is declared exactly once.
packer {
  required_plugins {
    vsphere = {
      version = ">= 1.4.0"
      source  = "github.com/hashicorp/vsphere"
    }
  }
}
