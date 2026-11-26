# Audit #443 ledger: every finding, its state, its evidence

Version: 1 · Applies to: v1.2.0-rc22 · Last reviewed: 2026-11-18

Issue [#443](https://github.com/mblanke/TrueNorth/issues/443) (Fable audit of rc18, 2026-11-05):
29 findings confirmed by a three-skeptic vote, 19 never checked (usage ran out), 19 refuted
(not listed here: refuted means the skeptics showed it is not a defect). This page gives each
confirmed or unchecked finding one state, with the evidence that decides it. Last-mile plan
item 2.12.

**How the state is decided.** Every finding has an oracle test in `tests/api/test_audit*_443*.py`
or `tests/worker/test_audit*_443*.py`, marked `audit443`. A test that passes **without** an
`xfail` marker means the fix is in. A `xfail(strict=True)` test means the bug is still open: it
turns into a failing XPASS the day the fix lands, which forces the marker out. So the
machine-readable list of open findings is:

```bash
grep -rn "mark.xfail" tests/api/test_audit*_443.py tests/worker/test_audit*_443.py tests/api/test_search_old_index.py
```

It lists 28 tests today. Two findings (f37, f40) have no test because no unambiguous secure
behaviour could be stated for them.

## Fixed (rc21, and after rc22 where the Fix column says so)

| Finding | Sev | Where | Fix |
|---|---|---|---|
| Lead instructor re-dates an approved class; hold and ranges stretch with no re-approval | high | `classes/router.py:349` | #492 |
| Admin rotation of a never-applied account settles as applied; certification flips to PASS | high | `range_credentials/service.py:450` | #495 |
| Snapshot delete after restore destroys the passwords a queued rotation needs | high | `routers/ranges.py:964` | #495 |
| Partial teardown recorded `destroyed` (two reports of one defect) | high | `worker/tasks.py:230` | #494 |
| Partial build recorded `ready` with no error | high | `worker/tasks.py:168` | #494 |
| Students read the full `provisioner_output` of a non-lab range | high | `schemas.py:147` | #490 |
| Lab token keeps working after the Student's account is deactivated | high | `routers/lab_sessions.py:146` | #491 |
| Any tenant's admin controls the platform-wide AI backend (two reports) | high / medium | `routers/ai_config.py:51` | #489 |
| cmi5 AU LRS proxy blocks the event loop while holding row locks | high | `cmi5/router.py:303` | #496 |
| "Start lab" during a teardown hands out an already-expired token | low | `routers/integrations.py:948` | commit `18c334df`, markers dropped in `d2d6d3bd` |
| Any tenant's admin can rewrite or delete platform-default auth zones | medium | `routers/auth_zones.py:37` | fix/443-auth-zone-platform-default (after rc22) |
| Forge with no `range_id` binds an exercise to any in-tenant range, including a Student's live lab; Run fires on it | medium | `routers/exercise_forge.py:238` | fix/443-forge-fallback-live-lab (after rc22) |
| `require_tenant_access` never sees its path parameter, so it passes every cross-tenant request (two reports) | low | `rbac.py:413` | fix/443-require-tenant-access (after rc22) |
| Public/global templates and scenarios are listed to other tenants but 404 by id (foreign rows read redacted: no accounts, no answer key) | low | `routers/templates.py:274` | fix/443-shared-content-by-id (after rc22) |
| Free text reaches indexed label values in search on a pre-v1.0 index (a Student's free text searches only an allow-list of visible fields; residual: a pre-2026-10-08 inject summary event put inject text in `message` and `event.action`, see the issue filed with this fix) | low | `main.py:775` | fix/443-search-free-text (after rc22) |
| Console outlived its sign-in token (closed at token expiry) | high | `ws_auth.py` | #442 (before the audit tests) |
| Two concurrent first results for one attempt both land (a partial unique index on `(tenant_id, source_enclave, attempt_id)` where there is no `supersedes_package_id`; the loser is a mapped refusal) | medium | `result_transfer/service.py:285` | fix/443-result-transfer-import (after rc22) |
| Cross-tenant `package_id` reuse ends in an unhandled `IntegrityError` (now a mapped refusal that does not say whose package it is) | low | `result_transfer/service.py:252` | fix/443-result-transfer-import (after rc22) |

## Open (strict xfail), by area

Severity is the finder's. "Test" is the oracle test that flips when it is fixed.

### Tenancy and shared policy

None open: all fixed (see the Fixed table).

### Results, outbound requests and credential revocation

| Finding | Sev | Where | Test |
|---|---|---|---|
| Server-side fetches of tenant-admin-controlled URLs (LTI JWKS, Moodle results pull) bypass `net_guard` | medium | `lti13.py:233` | `test_f32_lti13_jwks_fetches_go_through_net_guard` |
| A withdrawn Student keeps cmi5 launch, content and grading; only the LRS proxy checks withdrawal | medium | `cmi5/lms.py:211` | `test_f33_a_withdrawn_student_cannot_launch` |
| No limit on AU launches: a Student can flood the LRS | low | `cmi5/lms.py:340` | `test_f36_au_launches_are_limited` |
| First cmi5 launch races to a primary-key 500 | low | `cmi5/lms.py:228` | `test_f41_a_first_launch_racing_another_does_not_500` |
| Concurrent feed-bundle imports race to an unhandled `IntegrityError`; an older bundle can win | low | `content_feeds/service.py:204` | `test_f39_concurrent_bundle_imports_neither_crash_nor_lose` |
| A feed bundle takes ownership of online-origin entries, which the next bundle deletes | low | `content_feeds/service.py:243` | `test_f42_a_bundle_does_not_take_ownership_of_online_entries` |

### Console relay and WebSocket authentication

| Finding | Sev | Where | Test |
|---|---|---|---|
| WebSocket handshake refuses LTI session tokens; Moodle-launched Students cannot open a console (two reports) | medium | `ws_auth.py:55` | `test_ws_channel_accepts_an_lti_session_token`, `test_the_console_grant_an_lti_student_gets_opens` |
| Console grant token is written in clear to the API log by uvicorn's handshake line | medium | `middleware.py:617` | `test_the_grant_in_the_ws_handshake_line_is_redacted` |
| The live-console cap counts closed-but-unexpired grants: a Student is refused the fifth machine | medium | `console_relay/service.py:143` | `test_a_console_just_closed_frees_the_live_cap` |
| A Keycloak outage with a cold JWKS cache reads as "not allowed" (1008) not "unavailable" | low | `ws_auth.py:59` | `test_an_idp_outage_is_not_reported_as_not_allowed` |

### Classes

| Finding | Sev | Where | Test |
|---|---|---|---|
| Cancel racing Close-out overwrites a closed class to cancelled | medium | `classes/router.py:384` | `test_f2_cancel_may_not_overwrite_a_class_closed_concurrently` |
| Reject of a pending roll-call addition, racing an Approve, removes an admitted Student without freeing the seat | medium | `classes/service.py:476` | `test_f3_reject_may_not_remove_a_student_approved_concurrently` |
| A class's course can change after sessions were booked | medium | `classes/service.py:150` | `test_f4_a_booked_class_may_not_switch_course_behind_its_sessions` |
| A demoted assistant or observer keeps the class in `GET /classes` | low | `classes/access.py:35` | `test_f5_a_demoted_assistant_no_longer_sees_the_class_in_the_list` |
| Admission uses the UTC day for the class's last day, not the site zone | low | `classes/onboarding.py:100` | `test_f6_admission_keeps_the_site_day_not_the_utc_day` |

### Credentials and provisioning

| Finding | Sev | Where | Test |
|---|---|---|---|
| `force_settle` has no guard against a still-running worker task and leaves a wrong password on show | medium | `range_credentials/service.py:414` | `test_force_settling_while_the_worker_task_is_still_running_leaves_a_lie_behind` |
| Post-restore rotation is dispatched only from a `task_success` signal; a failed send strands every pending row | low | `credential_rotation.py:258` | `test_a_failed_post_restore_rotation_send_must_not_strand_the_rows` |
| `ensure_dvs_portgroup` reuses a same-named port group without re-checking VLAN or owner | low | `vsphere_infra.py:313` | `test_ensure_dvs_portgroup_refuses_a_same_named_foreign_port_group` |
| `reconcile_lab_vms` deletes VMs by annotation alone, outside the S5 recorded-ID rule | low | `lab_tasks.py:60` | `test_reconcile_does_not_delete_a_lab_vm_by_annotation_alone` |
| Recorded VMs vCenter no longer has under their moid are counted deleted, though they may be re-registered and running | medium | `vsphere_api.py:3211` | `test_a_re_registered_vm_still_running_is_counted_as_deleted` |

### Scheduler and capacity

| Finding | Sev | Where | Test |
|---|---|---|---|
| A class's own lab queues behind a self-serve lab that is itself waiting for capacity (two reports) | medium | `lab_sessions/service.py:412` | `test_a_classs_own_lab_starts_beside_a_queued_self_serve_lab`, `test_a_class_own_lab_is_not_queued_behind_a_self_serve_lab_waiting_for_capacity` |
| Ranges in `destroying` are not counted as running load, so a lab is admitted while old VMs still exist (two reports) | low | `capacity/service.py:117` | `test_a_range_being_destroyed_still_counts_as_committed`, `test_a_range_being_destroyed_still_counts_as_running_load` |
| Staff opening a Student's lab console restarts the idle clock and marks the lab active (two reports) | low | `lab_sessions/service.py:1276` | `test_staff_opening_the_console_does_not_touch_the_students_lab` (in both scheduler files) |
| The lab sweep runs once per uvicorn worker; the promised sweep-level lease does not exist | low | `lab_sessions/runner.py:5` | `test_a_sweep_pass_takes_a_lease_on_the_sweep` |

## No test: decision or evidence needed

| Finding | Why no test | What it needs |
|---|---|---|
| A hand-built `cmi5.xml` with `moveOn=Passed` and no `masteryScore` lets a Student self-certify (f37, `cmi5/rules.py:354`) | The secure behaviour was ambiguous at the base commit | Owner or SME: what should a package with no mastery score do? Then a test |
| The outbox receipt state machine has no `rejected` to `acknowledged` path, so the documented re-consume recovery is refused low-side (f40, `result_transfer/service.py:377`) | Same | Decide the recovery path in the result-transfer ADR, then test |
| ADR 0014 §18 "per-tenant quotas still apply" is a default-off knob no deploy artefact sets | A policy question, not a code defect | Owner: set a default, or record the limit |
| Idle close is defeated by a client-asserted heartbeat flag; a token-only loop keeps a lab alive for the 7-day maximum | Not stated as a test | Decide whether the server should verify activity, then test |
| Findings f45 and f48 of the audit's numbering | No finding text exists in the repository (card notes in `test_audit2_results_443.py`) | Recover the text from the audit journal, or drop them |

Not in this table, and not claimed: the 19 findings the skeptics **refuted**, and anything the
audit never reached. The second-pass oracle tests (`test_audit2_*`) cover the
findings that were never checked. Whether any part of the product was never audited at all is
not established here, and a fresh audit of the current release has not been done.

## Order I would fix them in

1. **Tenancy**: auth zones, Forge fallback, `require_tenant_access`, template reads. A tenant
   boundary is the thing a multi-tenant site cannot ship with. Needs `adversarial-reviewer`.
2. **Result transfer and revocation**: f34 and f38 (concurrency on the Secret side), f33
   (withdrawn Student), f32 (`net_guard`). These are the grading and data-integrity paths.
3. **Console relay**: the log redaction (a credential in a log file) and the LTI-token WebSocket
   (Moodle Students cannot open a console).
4. **Credentials**: `force_settle`, the stranded post-restore rotation.
5. **Classes** races, **scheduler** and **capacity**, then provisioning hardening (S5).

From #493 (follow-ups to the rc21 fix reviews, not listed above): a deactivated Student's lab is
not ended (capacity gate, security) and an admin class extension skips the capacity re-check. The
plan wants both fixed before GA; the rest of #493 goes after.

## Revision log

- v1, 2026-11-18: first ledger, from the oracle tests at `5520dbe0` (rc22).
- v2, 2026-11-18: public/global templates and scenarios by id fixed (fix/443-shared-content-by-id); open count 31.
- v3, 2026-11-18: search free text fixed (fix/443-search-free-text); open count 30; the tenancy group is closed.
- v4, 2026-11-19: result-transfer import fixed, f34 and f38 (fix/443-result-transfer-import); open count 28.
