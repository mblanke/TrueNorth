# TrueNorth repository entrypoint

Read `CLAUDE.md` and `docs/AGENTS.md` for repository operating guidance. Inspect
current code and configuration before relying on dated claims in `RESUME.md` or
`docs/current-state.md`. Follow the user's task scope and existing authorization.

## Task skills and specialist roles

Use `docs/agent-toolkit.md` to choose relevant skills from `SKILLS/tn-*/SKILL.md`.
Load only the skill needed for the work; do not load the entire catalogue by default.
Codex discovers the relative links under `.agents/skills/`; Claude Code uses
`.claude/skills/` and the specialist definitions in `.claude/agents/`.
The role files inherit model selection and do not grant additional tool permissions.
Use parallel agents only when the task authorizes it and ownership can be separated.

## Evidence and completion

For changes, run the appropriate targeted checks and the repository DoD gate.
Report blocked checks and their actual causes; do not substitute old passing counts.
Keep user changes intact. Confirm the actual interpreter and serving model, not an
alias, before relying on environment-specific claims.
