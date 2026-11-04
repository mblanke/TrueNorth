# Acceptance tests

| # | Test | Automated by | Pass condition |
|---|---|---|---|
| 1 | Reproducible | compiler | `tools/range compile` twice gives the same `plan_sha256`; `verify-plan` passes |
| 2 | Buildable | compiler | `BUILDABLE: yes`, no blockers (no TBD site fields, validated templates, recorded slots, owner set, not expired) |
| 3 | Preflight | `range_preflight` | No reserved VLANs, no VLAN collisions, templates tagged validated, datastore has capacity plus headroom |
| 4 | Idempotent | build twice | Second `tools/range build` changes no existing object (snapshots are skipped if present) |
| 5 | Isolated | `range_validate` | Self-test passes and every isolation target is unreachable from the probe |
| 6 | Connected | `range_validate` | Every VM reports its planned IPs; the probe reaches every gateway and listed service |
| 7 | Baseline | `range_snapshot` | Every VM has exactly one `tn-baseline` snapshot |
| 8 | Resettable | `tools/range reset` | Revert succeeds and validation passes again |
| 9 | No drift | `tools/range drift` | No findings right after build or reset |
| 10 | Clean teardown | `range_teardown` | No planned VM remains; `teardown.json` written; slot kept unless released |
| 11 | Evidenced | `tools/range manifest` | Verdict PASS; template provenance and hashes recorded |
| 12 | No secrets | review + hook | No credentials in plan, results, manifest, logs or repo |

The nightly canary (`ci/canary.sh`) runs tests 1 to 11 on `ranges/CANARY.yml`.
If the canary fails, do not build exercise ranges until the cause is known.
