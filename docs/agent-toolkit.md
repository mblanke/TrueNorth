# TrueNorth model toolkit

This repository now has **15 task skills and 15 new specialist agent definitions**.
The four existing Claude agent definitions are retained (19 total). The numbered
files directly under `SKILLS/` remain the legacy operating guidance.

## Locations and discovery

- `SKILLS/tn-*/SKILL.md`: canonical, model-independent skill instructions.
- `.claude/skills/tn-*`: relative links to those canonical folders for Claude Code.
- `.agents/skills/tn-*`: relative links for Codex repository skill discovery.
- `.claude/agents/tn-*.md`: 15 Claude Code specialist definitions, each preloading one skill.

The roles inherit the configured model; no remote provider or model name is pinned.
Claude agent files are not native Codex agent registrations. In Codex, invoke the
skills directly; in other harnesses, supply the relevant agent file and skill as task
instructions. The model server itself does not discover skills: the agent harness does.
Restart or refresh the harness after adding files and verify its skill/agent list.
On Taz, keep the relative symlinks when transferring or cloning this repository.

## Choose by task

| Task | Skill | Specialist |
|---|---|---|
| Epic planning | [tn-epic-planning](../SKILLS/tn-epic-planning/SKILL.md) | [tn-epic-planner](../.claude/agents/tn-epic-planner.md) |
| Navigation and user journeys | [tn-navigation-design](../SKILLS/tn-navigation-design/SKILL.md) | [tn-navigation-designer](../.claude/agents/tn-navigation-designer.md) |
| Angular feature delivery | [tn-angular-delivery](../SKILLS/tn-angular-delivery/SKILL.md) | [tn-angular-engineer](../.claude/agents/tn-angular-engineer.md) |
| API contract delivery | [tn-api-contracts](../SKILLS/tn-api-contracts/SKILL.md) | [tn-api-engineer](../.claude/agents/tn-api-engineer.md) |
| Tenant and identity boundaries | [tn-tenant-auth](../SKILLS/tn-tenant-auth/SKILL.md) | [tn-security-reviewer](../.claude/agents/tn-security-reviewer.md) |
| Reliable background operations | [tn-job-lifecycle](../SKILLS/tn-job-lifecycle/SKILL.md) | [tn-lifecycle-engineer](../.claude/agents/tn-lifecycle-engineer.md) |
| Reproducible database evolution | [tn-schema-migrations](../SKILLS/tn-schema-migrations/SKILL.md) | [tn-migration-engineer](../.claude/agents/tn-migration-engineer.md) |
| Scenario execution and evidence | [tn-scenario-evidence](../SKILLS/tn-scenario-evidence/SKILL.md) | [tn-scenario-validator](../.claude/agents/tn-scenario-validator.md) |
| Curriculum and qualification traceability | [tn-curriculum-traceability](../SKILLS/tn-curriculum-traceability/SKILL.md) | [tn-curriculum-engineer](../.claude/agents/tn-curriculum-engineer.md) |
| Telemetry and learning-record integration | [tn-telemetry-lms](../SKILLS/tn-telemetry-lms/SKILL.md) | [tn-integration-engineer](../.claude/agents/tn-integration-engineer.md) |
| Release and regression verification | [tn-release-verification](../SKILLS/tn-release-verification/SKILL.md) | [tn-release-engineer](../.claude/agents/tn-release-engineer.md) |
| Evidence-based incident diagnosis | [tn-incident-diagnosis](../SKILLS/tn-incident-diagnosis/SKILL.md) | [tn-incident-investigator](../.claude/agents/tn-incident-investigator.md) |
| Local model qualification | [tn-local-model-evaluation](../SKILLS/tn-local-model-evaluation/SKILL.md) | [tn-model-evaluator](../.claude/agents/tn-model-evaluator.md) |
| Independent change review | [tn-change-review](../SKILLS/tn-change-review/SKILL.md) | [tn-change-reviewer](../.claude/agents/tn-change-reviewer.md) |
| Accurate documentation and handoff | [tn-docs-handoff](../SKILLS/tn-docs-handoff/SKILL.md) | [tn-documentation-editor](../.claude/agents/tn-documentation-editor.md) |

Load the one or two skills relevant to the task. Do not paste all 15 into the system
prompt or launch all agents together. Specialist roles are reusable assignments, not
15 models that must be resident in GPU memory. A single coding model can execute
several roles sequentially. Parallel work needs explicit ownership of shared files.

## Example: navigation redesign for all roles

1. Use `tn-navigation-design` to establish learner, instructor, and administrator
   journeys, canonical destinations, and a concrete preview.
2. Use `tn-epic-planning` to split accepted changes into route/shell, exercise workspace,
   learner workspace, and administration slices with acceptance criteria.
3. Use `tn-angular-delivery` for the selected slice; add `tn-api-contracts` only if
   the user flow requires API changes.
4. Use `tn-release-verification` and `tn-change-review` to verify actual navigation,
   role boundaries, old links, and the backend-dependent states.

Example Codex prompt:

> Use $tn-navigation-design and $tn-epic-planning. Redesign TrueNorth navigation for
> learners, instructors, and administrators. Preserve deep links and permissions.
> Produce a clickable proposal and sequenced acceptance criteria before implementing
> the application changes.

Example Claude Code prompt:

> Use the tn-navigation-designer agent to map the current menus and propose coherent
> journeys for all three roles. Keep exercise preparation, delivery, and review in
> one exercise context. Return the route map and prototype for discussion.

For implementation, replace the last sentence with the concrete slice to implement.
These examples do not grant deployment or live infrastructure permissions.

## Verification and limits

Skills were written against the source inspected on 2026-09-26. Recheck pointers and
behavior when the repository changes; old test counts and runbooks are not current proof.
Structural validation checks naming/frontmatter, linked discovery folders, role-to-skill
references, and repository paths. It does not measure coding quality.

The current Mac checkout contains a Linux ELF executable at `.venv/bin/python`.
The DoD stops before tests: its preliminary message says Ruff is unavailable, while
running that interpreter directly exposes the actual executable-format mismatch.
Recreate a native environment or run the checks on Taz; do not copy a virtual environment
between operating systems or declare the application gate passed.

## Primary documentation

- [Codex skill locations and symlinks](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code project skills](https://code.claude.com/docs/en/skills)
- [Claude Code subagents and skill preloading](https://code.claude.com/docs/en/sub-agents)

