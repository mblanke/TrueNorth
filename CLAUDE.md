# TrueNorth Range — agent instructions

Operating model and roles: `docs/AGENTS.md`. Detailed skills: `SKILLS/` (index at
`docs/SKILLS.md`). Current operational state, modes and gotchas: `RESUME.md`.

## The gate

`./scripts/dod.sh` is the Definition of Done. It is real: it fails on undefined names,
redefinitions and syntax errors; it ratchets total ruff findings (`.dod-ruff-baseline`)
so debt can shrink but never grow; it runs the same pytest selection as CI
(`.github/workflows/ci.yml:37`). On success it records the passing tree in `.dod-pass`.
Web checks are opt-in with `DOD_WEB=1`.

Do not claim work is done until it passes. Run it with the venv: `bash scripts/dod.sh`.

## Writes are gated by model capability

A `PreToolUse` hook (`~/.claude/hooks/model-write-gate.py`) can refuse file edits. It
reads the **served** model from the session transcript, so it sees the engine that
actually answered, not the alias that was requested. Three independent rings:

1. **Capability** — only coding-capable models may write (`claude-*`, `qwen3-coder`,
   `qwen3-235b`, `glm-5.2`). `agent` / gpt-oss-120b is read-only here.
2. **Whole-file rewrite** — `Write` over an existing tracked file of >150 lines is
   refused. Use `Edit`. A whole-file write drops every line you did not re-emit.
3. **Post-compaction amnesia** — if the context compacted since you last read the file
   in full, read it again before editing. You are otherwise editing from a summary.

If you are refused: do not route around it with Bash, a shell redirect, or an MCP tool.
Either report the change you would make, or write a unified diff to
`.agent-patches/<name>.patch` and stop. Tune policy in `~/.claude/hooks/gate-policy.json`.

## Why this exists

On 2026-08-19 a session running on a non-coding local model rewrote `seed.py` and
`main.py` whole-file from post-compaction memory. The API was left serving zero routes,
`seed.py` lost every import, and `docs/` — which holds CAF material — was mounted
unauthenticated. The model never ran its own code, then wrote a document saying the work
was complete. Every existing guardrail was inert. Rings 2 and 3 are model-agnostic
because that failure mode is not exclusive to weak models.

## Repo facts

Python (FastAPI, Celery, SQLAlchemy) + Angular 17 (Material M3) + Terraform.
Tests: `.venv/bin/python -m pytest`. Lint: `.venv/bin/python -m ruff`.
The API is on `:4200` via nginx (`/api/`), not `:8000`.
