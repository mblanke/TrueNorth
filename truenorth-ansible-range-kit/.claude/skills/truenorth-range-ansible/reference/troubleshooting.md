# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `plan.json is stale` | Definition, blueprint, site, templates or allocation changed | `tools/range compile <ID>` |
| `BUILDABLE: NO` | Blockers listed under it | Fix each blocker; TBD site fields come from discovery |
| `slot allocation not recorded` | Compiled without `--write-allocation` | Compile with `--write-allocation` and commit `ranges/.allocations.json` |
| VLAN collision in preflight | Another portgroup uses a range VLAN | Refresh `discovery/live-network.json`, recompile; if the slot is recorded, a human must release and reassign it |
| `Template ... lacks tag` | Template not promoted | Follow golden-images.md |
| `Changing the device type is not possible` | Template NIC type differs from `nic_device_type` | Set `nic_device_type` in `config/templates.yml` to match, or rebuild the template |
| Probe self-test failed | Probe VM networking or guest credentials | Check the probe's IP and gateway in the plan and `results.json`; isolation is reported as inconclusive |
| `validate_certs: false` ignored for tag modules | `REQUESTS_CA_BUNDLE` in the environment overrides it in Python requests | Use the vCenter CA (preferred) or unset `REQUESTS_CA_BUNDLE` for the run |
| Teardown refuses: tagged VMs not in plan | A VM was tagged manually or the plan changed | Investigate. Do not force it. A human decides |
| Check mode skips clones | Folders or pools do not exist yet | Expected; a real run creates them first |
| `exists but has no 'tn-baseline' snapshot` | An earlier build stopped part-way | Teardown, then build again. Partial VMs are never reused |
| Approval rejected after recompile | Approvals are tied to the plan hash | Review the new plan; the human approves again |
| Isolation FAIL, result `reachable`, on a blocked target | A range firewall REJECTs (answers) instead of DROPping | Change the rule to DROP, or confirm the target really is reachable |
| `TNRANGE_ANSIBLE_ARGS ... not allowed` | Only vault options and -v are accepted | Move other settings into config or the playbook |

## Collections

The roles use `community.vmware` (portgroups, clone, guest ops, DRS, resource pools) and
`vmware.vmware` (folders, tags, snapshots, power). Several `community.vmware` modules are deprecated
in favour of `vmware.vmware` and will be removed in `community.vmware` 8.0. Pin both collections in
`collections/requirements.yml` and move modules over as replacements appear.

Tested module options against community.vmware 6.5.0 and vmware.vmware 2.11.0. Check-mode and
partial real runs were exercised against vcsim. The simulator does not support tagging with
current SDKs, VM cloning with current pyVmomi, or guest operations, so those paths need the lab canary.
