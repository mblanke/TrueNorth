# Release rollback rehearsal (2026-10-05)

Question: after the candidate's migrations have run on a production database, can we roll
the **code** back to the previous release (`main`) without touching the database?

Setup: a scratch PostgreSQL 16 database (dev-stack server), Alembic upgrade to the
candidate's head (`c0d1e2f3a4b5`; `DB_AUTO_CREATE=false`). Each step runs that release's
own API code (`git archive` of the branch) against the same database. The broker is
deliberately unreachable.

| Step | Code | What was done | Result |
|---|---|---|---|
| 1 | candidate (`5270cdf`) | template, 2 ranges, provision range A (202; operation `pending`, broker down), wiki space | as designed |
| 2 | **main (`083aeb7`), rolled back** | health; list/read ranges; create a range; provision range B; GET /wiki/spaces | health 200; both ranges listed, A reads `provisioning`; create 201; provision B 200 (old semantics, no operation row); wiki 404 (the feature is absent; its data is untouched) |
| 3 | candidate again, rolled forward | operations of A and B; wiki | A's `pending` operation intact (the API's redispatch loop sends it; the worker's fencing makes a late delivery safe); B has **no operation** (it was provisioned while rolled back) but its state updates normally; wiki space intact |

Conclusion: rolling back the code is safe on the upgraded schema, because the migrations
are additive. Two things follow:

- Range actions taken while rolled back leave no operation record. The range list shows
  their state but no progress text. Nothing needs repairing.
- Operations accepted but not yet sent before the rollback are sent after the roll
  forward. If the range has moved on in the meantime, the worker skips them
  (`worker/fencing.py`).

Downgrading the schema itself is also tested (`tests/api/test_migration_populated_upgrade.py`)
but is not needed for a code rollback.

## Rerun it

```
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5433/postgres \
  .venv/bin/python scripts/rehearse_rollback.py --old github/main --new hardening/integration-candidate
```

It archives each ref's `control-plane/api`, migrates a scratch database to the new head,
populates it, runs the old code on it, then the new code again, and drops the database.
Exit 0 means the five checks passed (2026-10-05: `"failed": []`).

