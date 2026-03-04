Follow `AGENTS.md` and `SKILLS.md`.

Rules:
- Use the PLAN -> IMPLEMENT -> VERIFY -> REVIEW loop.
- Keep model selection on Auto unless AGENTS.md role routing says to override.
- Never claim "done" unless DoD passes (`.\scripts\dod.ps1` or `./scripts/dod.sh`).
- Keep diffs small and add/update tests when behavior changes.
- Prefer reproducible commands and cite sources for generated documents.
