from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))
load_dotenv(backend_dir / ".env")

from sqlalchemy import text
from app.database import Database
from app.settings import Settings


async def inspect() -> None:
    settings = Settings.from_environment()
    db = Database(settings.database_url)

    async with db.sessions() as sess:
        print("=== DATABASE CONNECTION ===")
        print("Host:", settings.database_url.split("@")[1].split("/")[0])

        # Check pgvector extension
        ext_result = await sess.execute(
            text("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
        )
        exts = ext_result.fetchall()
        print("pgvector extension:", exts)

        # Check public tables
        tables_res = await sess.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        )
        tables = [t[0] for t in tables_res.fetchall()]
        print("\nTables in PostgreSQL public schema:")
        for t in tables:
            print(f"  [OK] {t}")

        # Check tasks table columns
        task_cols = await sess.execute(
            text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'tasks' ORDER BY ordinal_position")
        )
        print("\nColumns in 'tasks' table:")
        for c in task_cols.fetchall():
            print(f"  - {c[0]}: {c[1]}")

        # Check knowledge_chunks table columns
        kc_cols = await sess.execute(
            text("SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_name = 'knowledge_chunks' ORDER BY ordinal_position")
        )
        print("\nColumns in 'knowledge_chunks' table:")
        for c in kc_cols.fetchall():
            print(f"  - {c[0]}: {c[1]} ({c[2]})")

        # Check HNSW index
        idx_res = await sess.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'knowledge_chunks'")
        )
        print("\nIndexes on 'knowledge_chunks':")
        for idx in idx_res.fetchall():
            print(f"  - {idx[0]}: {idx[1]}")

        # Check row counts in each table
        print("\nLive Row Counts in PostgreSQL:")
        for tbl in tables:
            try:
                cnt = (await sess.execute(text(f"SELECT count(*) FROM \"{tbl}\""))).scalar()
                print(f"  - {tbl}: {cnt} rows")
            except Exception as e:
                print(f"  - {tbl}: error ({e})")

    await db.close()


if __name__ == "__main__":
    asyncio.run(inspect())
