# Golden images (Packer)

Optional. Use this if template building should live in this repo. If templates are built elsewhere,
delete this folder and record that process in `.claude/skills/truenorth-range-ansible/reference/golden-images.md`.

Shipped: `ubuntu-2404/`, a skeleton for `vsphere-iso` with a pinned ISO checksum, vmxnet3, and a
generalise step (machine-id, SSH host keys, leases, logs, history). Windows (sysprep), Kali and VyOS
images are TBD.

```bash
# Where to run: a build host that can reach vCenter and the ISO mirror
packer init packer/ubuntu-2404
packer validate -var-file=packer/vars/<site>.pkrvars.hcl packer/ubuntu-2404
packer build    -var-file=packer/vars/<site>.pkrvars.hcl -var build_id=$(date -u +%Y%m%d%H%M) packer/ubuntu-2404
# Expected: template tmpl-ubuntu2404-<build_id> in TrueNorth/Templates; packer-manifest-ubuntu2404.json written
# Validation: run the acceptance checks, then record provenance in config/templates.yml
# Rollback:   delete the new template; config/templates.yml still points at the previous one
```

Keep secrets out of var files. Pass `vcenter_password` and `guest_password` through environment variables
(`PKR_VAR_vcenter_password`), sourced from the same vault as Ansible.
