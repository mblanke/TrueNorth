---
name: tn-schema-migrations
description: "Create or review TrueNorth SQLAlchemy/Alembic changes; use for schema drift, fresh installation, upgrades, and data migrations."
---

# Reproducible database evolution

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/api/alembic`
- `control-plane/api/app/models.py`
- `control-plane/api/app/main.py`
- `tests/api/test_migration_completeness.py`
- `Makefile`

## Workflow

1. Confirm which Alembic configuration and revision tree the invoked command uses. Compare ORM changes with migration history and deployed-version assumptions.
2. Make new migrations self-contained with frozen schema definitions; importing mutable application metadata makes historical migrations change over time.
3. Plan upgrade behavior for both empty and existing databases, including nullability, defaults, indexes, foreign keys, backfills, and tenant ownership.
4. Do not casually rewrite already-applied revisions. Prefer forward repairs and explicitly explain any reconciliation required for historical drift.
5. Separate development seeding from production migration. Runtime create_all is not proof that the migration chain is complete.
6. Use disposable databases for checks; document irreversible transformations and recovery requirements.

## Verification

Run fresh upgrade and representative prior-version upgrade on PostgreSQL when available. SQLite smoke tests supplement but do not prove PostgreSQL compatibility.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Migration plus model changes, upgrade evidence, compatibility constraints, and rollback or recovery notes.

## Boundary

Never run a migration against a live database merely to validate a patch.

