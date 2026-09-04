"""Initial direct PostgreSQL schema and local users.

Revision ID: 20260904_0001
Revises:
"""
from typing import Sequence

from alembic import op

revision: str = "20260904_0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("""
          CREATE TABLE IF NOT EXISTS users (
            id uuid PRIMARY KEY,
            email varchar(320) NOT NULL UNIQUE,
            password_hash varchar(512) NOT NULL,
            roles jsonb NOT NULL DEFAULT '[\"viewer\"]'::jsonb,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
          )
        """)
        op.execute("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)")
        op.execute("""
          CREATE TABLE IF NOT EXISTS tasks (
            id varchar(32) PRIMARY KEY,
            pickup varchar(64) NOT NULL,
            destination varchar(64) NOT NULL,
            priority integer NOT NULL CHECK (priority BETWEEN 1 AND 100),
            status varchar(24) NOT NULL,
            assigned_robot_id varchar(32),
            created_at timestamptz NOT NULL DEFAULT now()
          )
        """)
        op.execute("CREATE INDEX IF NOT EXISTS tasks_created_at_idx ON tasks (created_at DESC)")
        op.execute("""
          CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id uuid PRIMARY KEY,
            source_name varchar(160) NOT NULL,
            content text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            embedding vector(384) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
          )
        """)
        op.execute("CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)")
    else:
        from app.models import Base
        Base.metadata.create_all(bind)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS users")
