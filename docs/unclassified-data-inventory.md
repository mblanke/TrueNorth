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

The first behavior ratchet is now executable in
`tests/api/test_unclassified_quiz_behavior_inventory.py`, with its accepted findings in
`docs/security/unclassified-behavior-baseline.json`. It submits a real quiz as a synthetic
pseudonymous Student and captures database rows, the HTTP response, xAPI statements and the
LTI background task. Its accepted finding set is now empty: the route seals the numeric grade
into the encrypted outbox before committing a pass/fail-only local projection, and fails the
submission closed if sealing fails. New readable answer/score paths fail the ratchet. This is
one bounded journey, not a deployment-wide compliance pass. Logs, traces, object storage,
OpenSearch, backups, support exports, Moodle-originated results, practical exercises and class
close-out remain to be covered.

Course/class close-out is now grade-free on Unclassified. The enrollment keeps `completed`
and `completed_at`, while `final_score`, `max_score` and `final_grade` are null. The same shared
service is used by direct TN close-out and Moodle-driven course completion; Secret behavior is
unchanged. Moodle's own raw result ledger is still a separate open inventory item. As an
interim control, the Moodle puller now refuses an entire signed page containing a quiz-grade
row on Unclassified before applying it, leaves the cursor at the page start, and writes only a
grade-free operator error. Secret ingestion is unchanged. A sealed Moodle producer and
pass/fail-only low-side projection remain required before grades can be enabled there.

The second executable behavior ratchet is
`tests/api/test_unclassified_exercise_behavior_inventory.py`, with its accepted findings in
`docs/security/unclassified-exercise-behavior-baseline.json`. It currently records 10 paths:
the shared exercise total/max, objective achieved/evidence, completion response, numeric xAPI
and LTI, and the competency worker that persists the shared score per participant. This is not
ready for a mechanical quiz-style scrub: exercises currently have one shared score and no
learner-specific attempt/release record. Participant attribution is the first required design
slice so absence of evidence cannot become an individual pass.

`imported_results` is excluded because ADR 0015 defines it as the Secret-only authoritative
grade store and the service refuses access on Unclassified. `noise_personas.display_name` is
synthetic exercise content, and `security_groups.ad_object_guid` identifies a directory group,
not a learner. Exceptions stay exact by table and column so similarly named fields elsewhere
are still findings.
