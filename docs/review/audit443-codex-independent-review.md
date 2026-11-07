# Audit #443 independent review

Observed 2026-11-07 against `github/main` at
`d5851f6d649a55b54f33e700be9d28be399e1302`.

This is a reviewer-side companion to the Taz brief, not a replacement for it and not
evidence that any finding has been fixed. Taz is iterating its brief independently.
The oracle patches referenced by that brief were not available in this checkout or on
GitHub when this review was written.

## Artifact acceptance status

The downloaded `TrueNorth-audit443-bug-brief.md` is materially more complete than the
three pasted fragments:

- 48 sequential finding headings (`f01` through `f48`);
- 48 `Where`, `Status`, `Severity view`, `Oracle test`, and evidence fields;
- 42 HOLD and 6 HOLD-CAVEAT verdicts, matching the issue #443 handoff;
- an appendix naming 51 proposed oracle tests across 11 files;
- two explicitly unreproduced findings (`f37`, `f40`).

It is not yet an accepted handoff. It still contains clipped source material and one
unbalanced Markdown code fence. Examples include lines 82, 114, 207, 236, 269, 302,
327, 371, 423, 444, 469, 495, 516, 557, 582, 604, 691, 716, 737, 762, 960, 1022,
1073, 1108, 1132 in the downloaded copy. The final Taz export should regenerate these
sections from the original return files rather than guessing the missing text.

There is also a direct contradiction for `f28`: its finding section says `no oracle`,
while the appendix names two tests for its template and scenario reads. The section and
manifest must agree.

## Root-cause normalization

The 48 audit rows are approximately 44 implementation groups. Preserve the source IDs,
but do not implement these duplicate or coupled rows as separate fixes:

| Group | Source rows | One implementation concern |
|---|---|---|
| Partial teardown | f04, f06 | A partial destroy is settled `destroyed` and releases reservations. |
| Platform AI configuration | f09, f19 | A tenant admin can mutate platform-wide AI state and route its sealed credential. |
| LTI WebSocket identity | f10, f11 | The HTTP handoff issues an LTI session that WebSocket identity resolution rejects. |
| Tenant dependency | f22, f29 | `require_tenant_access` cannot receive the path parameter it claims to enforce. |

Other findings may share a subsystem, but they have separate triggers and acceptance
criteria and should retain separate oracle assertions.

## Independent review of high-severity groups

The following conclusions come from reading the current source and its direct callers,
not from accepting the generated prose at face value.

| Finding | Verdict | Source observation |
|---|---|---|
| f01 class re-dating | Confirmed in source | `classes/router.py:update_class` permits changes on any open class; `classes/service.py:apply` writes both dates without approval or capacity re-evaluation; `classes/holds.py:holds` sizes the cluster-wide hold from `Class.end_date`. |
| f02 never-applied credential rotation | Confirmed in source | `range_credentials/service.py:rotate` has no `row.applied` guard for a running range. A failed worker change reaches `db_ops.settle_credential_change`, whose settlement values always set `applied=True` and may restore an unverified `previous_password`. |
| f03 snapshot deletion during restore rotation | Confirmed in source | `routers/ranges.py:delete_snapshot` refuses only creating/restoring snapshot states and immediately calls `forget_snapshot`; it does not check pending credential operations. The post-restore credential task is dispatched later from `task_success`. |
| f04/f06 partial teardown | Confirmed; duplicate rows | `worker/tasks.py:destroy_range` raises only for `status == "failed"`. Provisioner `outcome()` returns `partial` when some deletes succeed, while `_destroy_in_scope` leaves port groups/tags when errors exist. The task nevertheless records `destroyed` and calls `release_after_destroy`. |
| f05 provisioner output exposure | Confirmed in source | Student and observer roles hold `RANGE_READ`; `GET /ranges/{id}` returns `RangeOut`; `RangeOut` includes raw `provisioner_output`. `_readable_range` hides Student lab ranges but does not redact ordinary same-tenant range output. |
| f07 partial build | Confirmed in source | `provision_range` raises only for `failed`; `stored_output` drops `result.errors`; and `build_outcome` can return `ready` for a partial result with no `not_ready` entries or blocking isolation verdict. |
| f08 deactivated owner using a lab token | Confirmed in source | `_token_session` verifies the token, session owner, enrollment and session state, but never loads the owner `User` row or checks `is_active`/`deleted_at`. The signed-in path does check `User.is_active`. |
| f09/f19 tenant admin controlling platform AI | Confirmed; duplicate rows | The router is guarded only by `AI_CONFIG_WRITE`; `UserRole.admin` receives every permission; administrators are tenant-scoped; and the router never calls `is_platform_admin`. Its rows and outbound test/discovery behavior are intentionally platform-wide. |
| f30 blocking cmi5 LRS proxy | Confirmed in source | Async `_proxied` calls synchronous `lms.proxy`; authentication has already row-locked the registration and session; `lms.proxy` calls the synchronous `XapiLrsBackend.xapi_request`, which uses `httpx.Client` with a default ten-second timeout. |

These source observations establish credible defects. They do not validate the proposed
oracle implementations, exploitability claims, or every severity statement.

## Supporting checks

The following current tests passed. Their value here is to confirm neighboring behavior
and expose the coverage gap; none is a substitute for the missing oracle tests.

- `test_r1_a_classs_ranges_hold_capacity_between_its_sessions`: 1 passed.
- `tests/api/test_range_credentials_hardening.py`: 23 passed.
- vSphere destroy-neighbor test for a skipped foreign VM: 1 passed.
- lab token end/withdrawal neighbors: 4 passed.
- `tests/api/test_tenant_scoping_guard.py`: 31 passed.

One initial pytest node ID was wrong and collected zero tests; it was rerun by `-k` and
the intended vSphere test passed. No live Moodle, vCenter, LRS, Keycloak, broker, or
learner data was used.

## Required 07:00 handoff gate

Do not accept the final Taz package unless all of the following are true:

1. The brief names the exact full base SHA for each analysis pass.
2. Findings are `f01` through `f48` once each, with 42 HOLD and 6 HOLD-CAVEAT.
3. The four duplicate/coupled groups above are marked explicitly.
4. No command, evidence, test name, sentence, quote, or code fence is clipped.
5. `f28` consistently states whether its two appendix tests exist.
6. The result package contains all 11 referenced test files, not only their names.
7. A manifest maps every finding to a test or a recorded `no oracle` rationale.
8. Every test patch records its base SHA, patch hash, exact command, exit code, and
   `pytest -q -rxX` output.
9. Every proposed oracle is also run with `--runxfail` and demonstrates the intended
   failing assertion on the declared base.
10. No oracle mirrors the current implementation, weakens the desired behavior, mocks
    the unit under test, or passes without reaching the claimed branch.
11. `f37` and `f40` remain owner decisions/UNKNOWN until a secure behavior is specified;
    they are not converted into invented tests to improve the completion count.
12. The package contains actual served-model evidence and the independent/sceptical
    review output, not only an alias saying GLM-5.3-Flash.

## Fix sequence after oracle acceptance

1. Prevent secret/infrastructure disclosure and cross-tenant platform control: f05,
   f09/f19, f08, f32.
2. Stop silent terminal-state lies and credential divergence: f04/f06, f07, f02, f03,
   f15, f25, f31.
3. Restore learning and sign-in correctness: f10/f11, f21, f30, f33, f34, f38, f41.
4. Repair class/scheduler races and capacity behavior: f01, f12, f13, f14, f35,
   f43-f48.
5. Address remaining lower-risk consistency and hardening findings after owner decisions.

Auth, tenancy, credential and provisioning fixes require the repository's adversarial
review in addition to their oracle, targeted checks and the full Definition of Done gate.
