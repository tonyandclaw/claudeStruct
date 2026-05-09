"""add idempotency_key to runs

Revision ID: 66dadb6f7481
Revises: 0244691fc128
Create Date: 2026-05-08 10:58:35.438579

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '66dadb6f7481'
down_revision: Union[str, None] = '0244691fc128'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table because SQLite doesn't support ALTER TABLE
    # ADD CONSTRAINT directly — Alembic emulates by recreating the
    # table when the dialect needs it. No-op on Postgres.
    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        )
        batch.create_index(
            op.f("ix_runs_idempotency_key"), ["idempotency_key"], unique=False,
        )
        batch.create_unique_constraint(
            "uq_run_idempotency", ["org_id", "idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("uq_run_idempotency", type_="unique")
        batch.drop_index(op.f("ix_runs_idempotency_key"))
        batch.drop_column("idempotency_key")
