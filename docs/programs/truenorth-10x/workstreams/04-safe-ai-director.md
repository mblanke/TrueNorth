# Workstream 04 — safe AI exercise director

Status: blocked until workstream 02's event/effect envelope is accepted. Suggested branch:
`claude/10x-04-safe-ai-director`.

## Outcome

Use local AI to recommend exercise pacing or an approved inject while deterministic policy and
a named human retain authority over effects, grading and official records.

## Execution modes

- `scripted`: accredited, deterministic and preapproved.
- `adaptive`: select only from an approved action set inside declared bounds.
- `generative`: produce a proposal that cannot execute until a human approves it.

## First vertical slice

The model receives a sanitized timeline projection and may propose one pre-existing inject.
Persist prompt/context hashes, exact model identity, proposal, rationale, policy evaluation and
expiry. A lead instructor approves/rejects it. The existing deterministic injector performs
the effect with an idempotency key and kill/timeout path. Timeline replay shows proposal,
decision, execution and observed result.

Taz/GLM-5.3 used as a coding worker is separate from the runtime director model; do not
conflate their identities or permissions.

## Acceptance

- Prompt injection, malformed output and model outage produce no effect.
- Model cannot provision, attack, grade, cross an enclave or change an official record.
- Approval cannot be replayed for a different run/action/expiry.
- Duplicate execution changes nothing; timeout/cancel and stale intent are safe.
- Exact model/revision/template/policy and human decision are auditable.
- Instructor can run the exercise in scripted mode with AI entirely unavailable.

## Subsequent slices

Bounded adaptive tempo, NPC personas, evidence summarization and AAR drafting. Each capability
gets red-team fixtures, an explicit allowlist, human override and offline fallback.

## Skills

Primary: `tn-scenario-evidence`; add `tn-tenant-auth`, `tn-job-lifecycle`,
`tn-local-model-evaluation`, `tn-telemetry-lms` and `tn-change-review`. D05 is a human gate.
