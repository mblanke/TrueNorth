# vault/

Ansible Vault files live here and are git-ignored. Claude cannot read or write this folder (the guard hook blocks it).

Expected variables (encrypt with `ansible-vault create vault/vcenter.yml`):

```yaml
vcenter_hostname: vcenter.example.internal
vcenter_port: 443
vcenter_username: svc-truenorth-range@vsphere.local
vcenter_password: <secret>
guest_creds_linux:   {username: root, password: <secret>}
guest_creds_windows: {username: Administrator, password: <secret>}
```

Point the CLI at it:

```bash
export TNRANGE_ANSIBLE_ARGS="-e @vault/vcenter.yml --vault-id tn@prompt"
```

Use a least-privilege vCenter role for the service account. It needs to manage VMs, portgroups,
folders, resource pools, tags, snapshots and guest operations, but only under the range folder,
the range resource pool and the range switch.
