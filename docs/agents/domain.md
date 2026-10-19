# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

This is a single-context repo.

- **`docs/GLOSSARY.md`**: the one glossary for the whole repo (there is no root `GLOSSARY.md` and no `GLOSSARY-MAP.md`).
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. Numbering note: 0008 is reserved for an open branch (see ADR 0010), and 0007 is Greyspace.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo:

```
/
├── docs/
│   ├── GLOSSARY.md
│   └── adr/
│       ├── 0001-adapter-registry.md
│       └── 0002-interface-versioning.md
└── control-plane/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `docs/GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR 0012 (golden templates are host-local and key-free), but worth reopening because…_
