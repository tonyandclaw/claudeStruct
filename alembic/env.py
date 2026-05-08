"""Alembic migration environment.

This env.py is the entry point for all Alembic commands (revision,
--autogenerate, upgrade, downgrade). It configures the SQLAlchemy
engine from the ``DATABASE_URL`` env var (falls back to the same
default as ``claudestruct.server.db``) and imports all model modules
so SQLAlchemy's metadata is fully populated before autogenerate
compares the DB schema against it.

Usage::

    # Run all pending migrations
    cs serve migrate

    # Stamp the DB at a specific revision (e.g. after a fresh deploy)
    cs serve migrate --revision head

    # Create a new migration (autogenerate the delta)
    alembic revision --autogenerate -m "add foo column"

    # Show the current revision
    alembic current
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

# Import Base and all models so their metadata is registered.
# Import order matters for foreign-key resolution.
from claudestruct.server.db import Base  # noqa: F401
from claudestruct.server import audit  # noqa: F401
from claudestruct.server import billing  # noqa: F401
from claudestruct.server import models  # noqa: F401

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging.
# disable_existing_loggers=False so we don't silence loggers (e.g.
# claudestruct.notify) that were already configured before alembic ran —
# important when env.py is invoked from inside a test or a long-running
# server process that has its own logging set up.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    """Return the database URL.

    Priority: DATABASE_URL env var > CLI --db-url > default SQLite.
    """
    return os.environ.get(
        "DATABASE_URL",
        "sqlite:///./.claudestruct/server.db",
    )


def run_migrations() -> None:
    """Run migrations in 'online' mode."""
    # Override sqlalchemy.url from env so the placeholder in alembic.ini
    # doesn't produce a spurious warning.
    config.set_main_option("sqlalchemy.url", get_url())

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError(
        "Offline mode (alchemy migrate) is not supported; "
        "use 'cs serve migrate' which runs migrations online."
    )
else:
    run_migrations()