"""Database engine and session configuration.

Tuned for 1,200 concurrent users / 70,000 VMs:
  - PostgreSQL pool: 25 base + 50 overflow per worker (uvicorn typically runs 4-8 workers)
  - NullPool for Celery workers (one conn per task, returned immediately)
  - SQLite mode for tests (no pooling needed)
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://forge:forge@localhost:5432/forge")
_is_sqlite = DATABASE_URL.startswith("sqlite")
_is_celery = os.getenv("CELERY_WORKER", "").lower() in ("1", "true")

# -- Engine configuration based on runtime context ---------------------
_engine_kwargs: dict = {
    "echo": os.getenv("SQL_DEBUG", "").lower() == "true",
}

if _is_sqlite:
    # Test / dev: in-memory SQLite
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
elif _is_celery:
    # Celery workers: NullPool (one connection per task, no idle connections)
    _engine_kwargs["poolclass"] = NullPool
else:
    # API workers: connection pool sized for high concurrency
    # 25 base * 8 uvicorn workers = 200 base connections
    # 50 overflow * 8 = 400 burst connections
    # Total: 600 max connections per API pod, well within PgBouncer limits
    _engine_kwargs.update({
        "pool_size": int(os.getenv("DB_POOL_SIZE", "25")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "50")),
        "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT", "10")),
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", "1800")),
        "pool_pre_ping": True,
    })

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# Enforce statement timeouts on PostgreSQL to prevent long-running queries
if not _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_pg_options(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        # 30-second query timeout (tunable via env)
        timeout_ms = os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000")
        cursor.execute(f"SET statement_timeout = '{timeout_ms}'")
        # Optimize for read-heavy workloads
        cursor.execute("SET work_mem = '16MB'")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """Yield a database session, closed after request."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_health() -> bool:
    """Quick health check: execute SELECT 1."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False