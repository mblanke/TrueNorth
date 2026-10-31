"""Build an expensive fixture's database once per test run, then replay its rows.

Importing the whole catalogue and every course file costs about three seconds, and the
`spine` fixtures did it for each of the ~50 tests that read it. `restore` runs the builder
against a scratch in-memory database the first time a key is asked for, keeps the rows,
and inserts them into the test's own session after that. Each test still gets private,
rolled-back data, and the builder code is the same code that built it before.
"""

from collections.abc import Callable

from app.db import Base
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_ROWS: dict[str, list[tuple]] = {}


def _snapshot(build: Callable) -> list[tuple]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    with sessionmaker(bind=engine, autoflush=False)() as db:
        build(db)
        db.commit()
        return [
            (table, [dict(r) for r in db.execute(select(table)).mappings()])
            for table in Base.metadata.sorted_tables
        ]


def restore(db, key: str, build: Callable):
    """Fill `db` with what `build(session)` leaves behind; `build` runs once per `key`."""
    if key not in _ROWS:
        _ROWS[key] = _snapshot(build)
    for table, rows in _ROWS[key]:
        if rows:
            db.execute(insert(table), rows)
    db.flush()
    return db
