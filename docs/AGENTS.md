# Agent Operating System (Auto-first) — TrueNorth Range

Default: use **Auto** model selection.
Only override the model when a role below says it is worth it.

## Prime directive
- Never claim "done" unless **DoD passes**.
- Prefer small, reviewable diffs.
- If requirements are unclear: stop and produce a PLAN + questions inside the plan.
- Agents talk; **DoD decides**.

## Always follow this loop
1) PLAN: goal, constraints, assumptions, steps (≤10), files to touch, test plan.
2) IMPLEMENT: smallest correct change.
3) VERIFY: run DoD until green.
4) REVIEW: summarize changes, risks, next steps.

## DoD Gate (Definition of Done)
Required before "done":
- Windows: `.\scripts\dod.ps1`
- macOS/Linux: `./scripts/dod.sh`

If DoD cannot be run, say exactly why and what would be run.

## Terminal agent workflow (Copilot CLI)
1) Plan: draft plan + file list + test plan.
2) Build: implement smallest slice.
3) Steer: when stuck, ask for next action using current errors/logs.
4) Verify: run DoD until green.

Rules:
- Keep diffs small.
- If the same error repeats twice, switch to Reviewer role and produce a fix plan.

## Role routing (choose a role explicitly)

### Planner
Use when: new feature, refactor, multi-file, uncertain scope.
Output: plan + acceptance criteria + risks + test plan.

### UI/UX Specialist
Use when: screens, layout, copy, design tradeoffs, component structure.
Output: component outline, UX notes, acceptance criteria.

### Coder
Use when: writing/editing code, plumbing, tests, small refactors.
Rules: follow repo conventions; keep diff small; add/update tests when behavior changes.

### Reviewer
Use when: before merge, failing tests, risky changes, security-sensitive areas.
Output: concrete issues + recommended fixes + risk assessment.

## Non-negotiables
- Do not expose secrets/tokens/keys. Never print env vars.
- No destructive commands unless explicitly required and narrowly scoped.
- Do not add new dependencies without stating why + impact + alternatives.
- Prefer deterministic, reproducible steps.
- Cite sources when generating documents from a knowledge base.

## Repo facts
- Primary stack: Python (FastAPI, Celery, SQLAlchemy), Angular 17+ (Material M3), Terraform
- Package manager: pip (Python), npm (Angular)
- Test command: pytest -q (Python), ng test (Angular)
- Lint/format command: ruff check . && ruff format --check . (Python), ng lint (Angular)
- Build command: docker compose -f infra/platform/docker/compose.dev.yml up --build
- Deployment: Docker Compose (dev), Proxmox + Terraform (ranges)

## Claude Code Agents (optional)
- `.claude/agents/architect-cyber.md` — architecture + security + ops decisions for cyber range platform.
- `.claude/agents/scenario-engineer.md` — scenario YAML, injectors, validators, MESL timelines, QSP/CFITES objective mapping; authoring or reviewing exercise content and its scoring.
- `.claude/agents/adversarial-reviewer.md` — argues against merging; use before landing anything touching auth, scoring, provisioning, the curriculum spine, or multi-tenancy.
- `.claude/agents/test-engineer.md` — pytest and Angular/Karma coverage, regression and contract tests, environment-vs-code failure diagnosis; use when tests fail or coverage is thin.

### ARC² (AI Rapid Course Creator)
- `.claude/commands/arc2.md` — `/arc2 <request> | --resume <slug> [accept|<feedback>]`: free-text course request → staged, QA-checked TrueNorth bundle + cmi5 package candidate under `build/arc2/<slug>/`. Two human gates (outline, preview); for preview feedback the orchestrator picks the stage to route it to. The only caller of the agents and of init/merge/check/gate; never commits, applies, provisions or imports.
- `.claude/agents/arc2-content-architect.md` — `01-blueprint/`: request, course, observable objectives, critical events. Binds a PO only when its crosswalk row is `todo`/`example` and no course already delivers it (otherwise `po_candidates` for Standards); crosswalk critical events are verbatim `;`-items of the row.
- `.claude/agents/arc2-code-generator.md` — `02-content/`: course YAML, per-module `course-config.json` and learner pages; objective coverage is counted from its `files[]` entries.
- `.claude/agents/arc2-range-engineer.md` — `03-range/`: a reused range or a new vSphere `range.tf` (fmt/validate only), and the defanged inject timeline with noise floor; offensive steps are `AUTHOR-REQUIRED`.
- `.claude/agents/arc2-artifact-creator.md` — `04-artifacts/`: rubric, deliverable template, variant B, `xapi.json` (the only place NICE/DCWF identifiers go), instructor-only directory.
- `.claude/agents/arc2-sensor-gateway.md` — `05-sensor/`: must-pass crit validators that fail on zero hits, a `manual_ack` summative that is never auto-scored, the engine scenario, the xAPI telemetry map.
- `.claude/agents/arc2-qa-tester.md` — `06-qa/report.md` and the preview digest, from `check --dry-run`. `check` itself produces the findings and routes rework; the qa-tester owns no manifest keys.
- `.claude/agents/arc2-package-builder.md` — `07-bundle/`: runs `arc2.cmi5 package` (the package is derived, never hand-written), lays out the TrueNorth bundle by destination, writes `PROMOTE.md`; only after QA pass and preview accept.
- `tools/arc2/` — the run contract, run as `PYTHONPATH=tools .venv/bin/python -m arc2.check …`: `manifest.schema.json`; `check.py` (init/merge/check [--dry-run]/gate/status); `qa.py` (repo content rules, called by `check`); `cmi5.py` (ids/package/validate); `au/` (AU runtime and templates); `vendor/` (`CourseStructure.xsd`, fetched by a human). Tests in `tests/arc2/`.
- Staging rule: runs write only under `build/arc2/` (gitignored). Promotion is a human PR following `07-bundle/PROMOTE.md` (destinations under `content/` and the content pack). A package candidate with open human actions is not an approved release. Product boundary and later slices: `docs/arc2-course-studio.md`.
