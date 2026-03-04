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
