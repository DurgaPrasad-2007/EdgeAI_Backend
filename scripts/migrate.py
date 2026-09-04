from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from app.database import async_engine_configuration


def run_migrations() -> None:
    load_dotenv(backend_dir / ".env")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL environment variable is not set", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Running database migrations on: {database_url.split('@')[-1] if '@' in database_url else 'local'}")
    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    try:
        command.upgrade(alembic_cfg, "head")
        print("[INFO] Migrations successfully applied (head).")
    except Exception as exc:
        print(f"[ERROR] Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)


async def verify_tables() -> None:
    load_dotenv(backend_dir / ".env")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return
    engine_url, engine_options = async_engine_configuration(database_url)
    engine = create_async_engine(engine_url, **engine_options)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
        ))
        tables = [row[0] for row in result.fetchall()]
        print(f"[INFO] Verified public tables: {', '.join(tables)}")
    await engine.dispose()


if __name__ == "__main__":
    run_migrations()
    if os.getenv("DATABASE_URL", "").startswith("postgresql"):
        asyncio.run(verify_tables())
