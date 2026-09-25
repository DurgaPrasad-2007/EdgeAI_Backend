"""Audit trail table and the task payload columns the ORM model already writes.

Revision ID: 20260925_0002
Revises: 20260904_0001
"""
from typing import Sequence

from alembic import op

revision: str = "20260925_0002"
down_revision: str | None = "20260904_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        from app.models import Base
        Base.metadata.create_all(bind)
        return
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS payload_kg double precision NOT NULL DEFAULT 150")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS payload_size varchar(32) NOT NULL DEFAULT 'medium'")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS urgency varchar(32) NOT NULL DEFAULT 'standard'")
    op.execute("""
      CREATE TABLE IF NOT EXISTS audit_events (
        id serial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        actor varchar(320) NOT NULL,
        category varchar(16) NOT NULL,
        event_type varchar(16),
        sim_time varchar(16),
        message text NOT NULL
      )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_events")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS urgency")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS payload_size")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS payload_kg")
