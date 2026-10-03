# Packer skeletons (superseded)

These `*.pkr.hcl` files are **early review skeletons**, one per catalogue image. They are
intentionally minimal (empty `boot_command`, `shutdown_command`, no provisioners) and are
**not buildable** as-is.

The buildable, self-contained vSphere Packer library now lives at:

    infra/vsphere/packer/

Use that directory for all real builds (`packer validate`, `packer build`, `build.sh`). It
has the shared variables, the answer files (Autounattend / autoinstall / preseed /
kickstart), the role scripts for derived images, and the Content Library output wiring.

These skeletons are **kept, not deleted** — they remain a compact per-image reference of
the catalogue specs (vCPU/RAM/disk, base ISO, sensor flag, derived-vs-iso) and a sketch of
the vsphere-iso/vsphere-clone split. When the two disagree, `infra/vsphere/packer/` is
authoritative.
