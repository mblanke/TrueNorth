# Unclassified learner-data inventory

This is the live remediation inventory for the COTE Unclassified-first boundary. It does
not certify the current application as safe for real learner identity or grades. It prevents
new obvious persistence from appearing while existing paths are converted to pseudonymous
identity, pass/fail retention and sealed low-to-high result transfer.

Run:

```bash
.venv/bin/python scripts/unclassified_data_inventory.py --check
```

The check inspects the registered SQLAlchemy schema. It fails when a new column can directly
store a learner identity attribute, submitted answers or a readable grade and that column is
not in `docs/security/unclassified-data-baseline.json`. Removing a finding passes without
editing the baseline. Baseline additions require security review and must explain why the new
Unclassified persistence is unavoidable.

## Confirmed remediation groups

| Group | Current stores | Required end state |
|---|---|---|
| Account and registration | `users`, `registration_requests` | Pseudonymous Unclassified account with no name, operational email, service number, rank, unit or directory identity |
| Class admission | `class_roll_call.email` | Opaque subject admission or a separately approved contact workflow outside the training record |
| Quiz scoring | `quiz_attempts`, `module_progress`, `competency_auto_assessments` | Seal numeric result in memory; retain pass/fail only; do not retain answers or per-competency numeric mappings |
| Other learning results | `enrollments`, `exercises`, `external_activities`, `moodle_result_records` | Apply the same pass/fail plus sealed-transfer policy, with Secret as grade authority |
| Collaboration and acknowledgement | `analyst_annotations`, `shared_commands`, `acknowledgement_signatures` | Replace copied names/emails with an enclave-local pseudonym or approved opaque subject reference |

The scanner covers database schema only. API responses, xAPI/LRS statements, LTI grade
passback, logs, traces, object storage, OpenSearch, backups and support exports need separate
behavior tests. The quiz trace already confirms that its current submission response, xAPI
statements and LTI task also carry numeric grades; those remain blockers even after database
columns are converted.

`imported_results` is excluded because ADR 0015 defines it as the Secret-only authoritative
grade store and the service refuses access on Unclassified. `noise_personas.display_name` is
synthetic exercise content, and `security_groups.ad_object_guid` identifies a directory group,
not a learner. Exceptions stay exact by table and column so similarly named fields elsewhere
are still findings.
