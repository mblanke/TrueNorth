# TrueNorth module integration

After a build or reset, `build/<ID>/integrations.json` describes the range for other
TrueNorth modules:

```json
{
  "range_id": "EXCOURSE",
  "plan_sha256": "...",
  "integrations": {"range_activity": {"enabled": true, "profile": "office-day"}, ...},
  "source_scenario": {"id": "SCN-0001", "techniques": ["T1566.001"]},
  "instances": [{"id": "EXCOURSE-S01", "networks": [...], "vms": [...]}]
}
```

| Integration | Consumer | What it needs from the range | Status |
|---|---|---|---|
| `range_activity` | User emulation and traffic generation | Workstation VMs, addresses, domain | Contract TBD |
| `greyspace` | Greyspace module | Network list, gateway, which segment is "internet" | Contract TBD |
| `arc2` | ARC² content generation | Scenario ID, techniques, VM roles | Contract TBD |

Rules:
- The range playbooks do not push content into integrations. They publish `integrations.json`
  and the consuming module acts on it. This keeps range builds deterministic.
- An integration must not add a route out of the range. Anything that needs outside access is
  an isolation exception and needs explicit approval and a design record.
- When a contract is agreed, record it here and add a schema check to `tnrange/schema.py`.
