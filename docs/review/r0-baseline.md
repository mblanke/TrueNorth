# R0 baseline: `main@0fa60ee`

The baseline for the first delivery unit of `docs/review/codereview1.md` (rev 3, section 5). Every later R-unit branches from `main` and shows its failing test against this baseline.

## Toolchain

| | Version |
|---|---|
| Python | 3.11 (CI `PYTHON_VERSION`; local 3.11.16) |
| Node | 22 (CI `NODE_VERSION`, now also `.nvmrc`) |
| ruff | 0.16.3 (newer versions move `.dod-ruff-baseline`) |
| PostgreSQL | 16 (CI service; local throwaway container) |
| Test dependencies | `requirements-test.txt`, the single source for CI and the DoD venv |

## What R0 changes

- **Tests never reach a real broker.**
  - Before this change, `tests/conftest.py` defaulted `REDIS_URL` to `localhost:6379/15`, so on a developer machine every run sent real Celery messages into the dev stack's Redis. While this unit was being verified, another worktree's test run on `main`'s conftest added about ten messages to db 15 in two minutes.
  - `REDIS_URL` now points at an address that refuses connections, and the autouse fixture `no_real_broker` records every `send_task` instead of publishing it.
  - `tests/test_no_real_broker.py` fails on `main`'s conftest and passes here.
  - Tests that need a real Redis use `TEST_REDIS_URL`.
- **A shared PostgreSQL fixture.**
  - `postgres_engine` gives each test its own database: a copy of one template built by `alembic upgrade head`, so the schema is the production one, not `create_all`'s.
  - A test can open as many sessions as a race needs.
  - Without `TEST_POSTGRES_ADMIN_URL` the tests skip; CI now runs a `postgres:16` service and sets that variable.
  - `tests/api/test_postgres_fixture.py` proves two things:
    - the chain has a single head and the copy is at it;
    - the one-accepted-release index from the migration is enforced.
  - This fixture is the base for CR1-13; R2 and R3 build their concurrency tests on it.

## Gate result on this branch

`TEST_POSTGRES_ADMIN_URL=… bash scripts/dod.sh`:

- DoD PASS: 1621 passed, 1 skipped, 3 xfailed.
- ruff debt 27, at its baseline of 27.
- Without PostgreSQL: DoD PASS, 1618 passed, 4 skipped (the PostgreSQL tests), 3 xfailed.
- Web checks (`DOD_WEB=1`) were not run; R0 changes no web code.

## Running the PostgreSQL tests locally

Use a throwaway container, never the dev stack's database:

```bash
docker run -d --rm --name tn-test-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 postgres:16
```

```bash
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://postgres@127.0.0.1:55432/postgres bash scripts/dod.sh
```
