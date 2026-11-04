# Scenario to range pipeline

Turns an exercise scenario (for example from the ATT&CK exercise scenario builder) into a range definition.

```bash
tools/range scenario scenarios/SCN-0001.yml --id SCN1 --expires 2026-12-31 --owner <name>
tools/range validate SCN1
tools/range compile SCN1 --write-allocation
```

## Input contract

TBD: align with the scenario builder owner.

```yaml
scenario:
  id: SCN-0001
  title: Phishing to domain compromise
  techniques: [T1566.001, T1059.001, T1003.001]
  assets:
    - {role: domain_controller, count: 1}
    - {role: workstation, count: 5}
    - {role: web_server, count: 1}
    - {role: attacker, count: 1}
```

## Mapping

`config/role_map.yml` maps each asset role to a blueprint, an alias and the count parameter.
Assets that map to the same blueprint and alias are merged. Integer parameters a scenario does
not mention are set to their minimum, so the range never contains unrequested hosts. A core
router joining every network is added when there is more than one network. An attacker asset
is required, because the probe must run from inside the range.

Techniques are carried into `source_scenario` in the definition, the plan, the manifest and
`integrations.json`, so ARC² and reporting can link a range to the scenario it serves.

An unmapped role is an error, not a silent skip. Add a blueprint and a mapping, or change the scenario.
