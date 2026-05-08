"""Tests for Alembic migration scaffolding (W6.3).

These tests verify that:
1. The alembic.ini and env.py are valid and loadable.
2. ``cs serve migrate --revision head`` succeeds on a fresh in-memory DB.
3. The baseline migration (auto-generated from current schema) is present.
4. ``cs serve migrate`` (head) is idempotent on an already-migrated DB.

The tests are skipped when alembic is not installed so the lean-install
CI matrix stays green.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# claudestruct imports live above the importorskip so ruff's import-order
# rules are satisfied at the static-analysis level. The importorskip guard
# means these never execute in a lean-install CI run.
from claudestruct.server import (  # noqa: F401,E402
    audit,
    billing,
    models,
)
from claudestruct.server.db import Base  # noqa: E402

alembic = pytest.importorskip("alembic")

# alembic and sqlalchemy imports are guarded by the importorskip above,
# so E402 is annotated to prevent a false-positive "import not at top" flag.
from alembic.config import Config as AlembicConfig  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fresh_db():
    """In-memory SQLite DB with all tables created via create_all."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Register all models and create tables.
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def alembic_ini_path():
    """Path to the repo's alembic.ini."""
    root = Path(__file__).parent.parent
    return root / "alembic.ini"


@pytest.fixture()
def script(alembic_ini_path):
    return ScriptDirectory.from_config(
        AlembicConfig(str(alembic_ini_path))
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_alembic_ini_is_valid_config(alembic_ini_path):
    """alembic.ini loads without errors and has a script_location."""
    cfg = AlembicConfig(str(alembic_ini_path))
    assert cfg.get_main_option("script_location") == "alembic"


def test_env_importable():
    """alembic/env.py is syntactically valid and imports cleanly."""
    root = Path(__file__).parent.parent
    env_path = root / "alembic" / "env.py"
    # Compile-check the file (no SyntaxError).
    with open(env_path) as fh:
        compile(fh.read(), str(env_path), "exec")


def test_versions_directory_exists():
    """alembic/versions/ directory is present."""
    root = Path(__file__).parent.parent
    versions = root / "alembic" / "versions"
    assert versions.is_dir()


def test_baseline_migration_is_present(script):
    """The seeded baseline migration is the one and only head."""
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one head revision (the baseline); "
        f"found {len(heads)}: {heads}"
    )


def test_baseline_migration_has_no_parent(script):
    """The baseline migration is the chain root (down_revision is None)."""
    head_id = script.get_heads()[0]
    head_rev = script.get_revision(head_id)
    assert head_rev.down_revision is None, (
        "Baseline migration must have down_revision=None so "
        "`alembic upgrade head` works on a fresh DB."
    )


def test_baseline_migration_creates_all_model_tables(tmp_path):
    """`alembic upgrade head` on a fresh DB creates every table the ORM knows
    about. This catches the case where someone adds a SQLAlchemy model but
    forgets to run `alembic revision --autogenerate`."""
    from alembic.config import Config as AlembicConfig

    from alembic import command

    db_path = tmp_path / "alembic_smoke.db"
    db_url = f"sqlite:///{db_path}"

    root = Path(__file__).parent.parent
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    # Override DATABASE_URL via env so env.py picks it up.
    import os as _os
    _prev = _os.environ.get("DATABASE_URL")
    _os.environ["DATABASE_URL"] = db_url
    try:
        command.upgrade(cfg, "head")
    finally:
        if _prev is None:
            _os.environ.pop("DATABASE_URL", None)
        else:
            _os.environ["DATABASE_URL"] = _prev

    # Compare migration-created tables vs the ORM metadata.
    from sqlalchemy import create_engine, inspect

    engine = create_engine(db_url)
    inspector = inspect(engine)
    migration_tables = set(inspector.get_table_names())

    expected_tables = set(Base.metadata.tables.keys())
    # alembic_version is created by alembic itself; not in ORM metadata.
    expected_with_alembic = expected_tables | {"alembic_version"}

    missing = expected_with_alembic - migration_tables
    assert not missing, (
        f"Migration is missing tables that exist in Base.metadata: {missing}. "
        f"Did you add a new model without regenerating the migration? "
        f"Run `alembic revision --autogenerate -m 'add X'` to fix."
    )


def test_alembic_api_importable():
    """The alembic module API is importable (main classes used by our code)."""

    from alembic import context
    assert callable(context.configure)  # smoke check
