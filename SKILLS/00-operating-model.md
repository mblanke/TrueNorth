# Operating Model
## Default cadence
- Prefer iterative progress over big bangs.
- Keep diffs small: target <= 300 changed lines per PR unless justified.
- Update tests/docs as part of the same change when possible.
## Working agreement
- Start with a PLAN for non-trivial tasks.
- Implement the smallest slice that satisfies acceptance criteria.
- Verify via DoD.
## Stop conditions (plan first)
Stop and produce a PLAN (do not code yet) if:
- scope is unclear
- more than 3 files will change
- data model changes
- auth/security boundaries
- performance-critical paths
