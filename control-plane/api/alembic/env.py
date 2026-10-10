"""Alembic environment configuration."""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add parent dir to path for model imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Imported to register their tables on Base.metadata.
from app import (  # noqa: F401
    course_publishing,
    course_releases,
    detections,
    lab_sessions,
    lti_identity,
    models,
    models_tickets,
    models_wiki,
    moodle_results,
    network_inventory,
    noise,
    range_leases,
    range_ops,
    scenario_runs,
    xapi_identity,
)
from app.db import Base
from app.greyspace import models as greyspace_models  # noqa: F401
from app.notifications import models as notification_models  # noqa: F401

config = context.config

# Override sqlalchemy.url from environment if set
db_url = os.getenv("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
