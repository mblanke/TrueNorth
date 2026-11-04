# Blueprints

A blueprint is a tested building block: networks plus VMs with typed parameters.
File: `blueprints/<name>.yml`. `blueprint.name` must equal the file name.

```yaml
blueprint:
  name: dmz
  description: Exposed service segment with Linux web servers.
  params:
    web_servers: {type: int, default: 1, min: 0, max: 20}
    template:    {type: str, default: ubuntu2404}
networks:
  - name: net
    gateway: true
vms:
  - name: web{nn}
    count: ${web_servers}
    template: ${template}
    role: web_server
    cpu: 2
    memory_gb: 2
    disk_gb: 40
    nics: [net]
```

Rules:
- Parameter types are `int`, `str`, `bool` and `list`. Every parameter needs a default. Use `min`/`max` on ints.
- `${param}` as a whole value keeps its type (lists stay lists). Inside a string it is converted to text.
  In flow mappings, quote it: `vars: {domain: "${domain}"}`.
- `count: 0` removes the VM. `count > 1` needs `{nn}` in the name (01, 02, ...).
- Local network names resolve inside the blueprint. Use `@<alias>-<net>` for networks from other blueprints.
- Keep blueprints small and single-purpose. Compose them in the range definition.

## Shipped blueprints

| Blueprint | Networks | VMs | Key params |
|---|---|---|---|
| `ad_forest` | `lan` | `dc{nn}`, `fs{nn}`, `ws{nn}` | `domain`, `domain_controllers`, `file_servers`, `workstations` |
| `dmz` | `net` | `web{nn}` | `web_servers` |
| `attacker_enclave` | `net` | `op{nn}` (first is the default probe) | `operators` |
| `core_router` | none | `rtr01` takes `.1` on each routed network | `routed_networks` (list of `@` names) |

## Adding a blueprint

1. Write `blueprints/<name>.yml`.
2. Add it to a test range and run `tools/range validate` and `compile`.
3. Add a unit test in `tests/test_tnrange.py` (compiles, addressing as expected, bounds enforced).
4. If it introduces a new role, add `playbooks/roles/range_guest_config/files/<os>/<role>.(sh|ps1)`
   or state in the blueprint description that the role needs no configuration.
5. If scenarios should use it, map the role in `config/role_map.yml`.
