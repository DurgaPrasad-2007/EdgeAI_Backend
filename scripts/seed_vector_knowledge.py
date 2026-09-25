from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from dotenv import load_dotenv
load_dotenv(backend_dir / ".env")

from sqlalchemy import select
from app.database import Database
from app.models import KnowledgeChunkModel
from app.knowledge import WAREHOUSE_SOPS, text_to_embedding
from app.settings import Settings


async def seed_vector_database() -> None:
    settings = Settings.from_environment()
    print(f"[INFO] Connecting to PostgreSQL ({settings.database_url.split('@')[-1] if '@' in settings.database_url else 'local'})...")
    db = Database(settings.database_url)

    async with db.transaction() as session:
        existing = list((await session.scalars(select(KnowledgeChunkModel))).all())
        existing_names = {item.source_name for item in existing}

        added = 0
        for name, content, meta in WAREHOUSE_SOPS:
            if name not in existing_names:
                emb = text_to_embedding(content)
                session.add(
                    KnowledgeChunkModel(
                        id=uuid.uuid4(),
                        source_name=name,
                        content=content,
                        metadata_json=meta,
                        embedding=emb,
                    )
                )
                added += 1

        print(f"[INFO] Seeding completed: {added} new documents added ({len(existing) + added} total in pgvector).")

    # Verify pgvector cosine similarity retrieval
    async with db.sessions() as session:
        query = "corridor C-14 emergency choke point yielding"
        q_emb = text_to_embedding(query)
        statement = (
            select(KnowledgeChunkModel)
            .order_by(KnowledgeChunkModel.embedding.cosine_distance(q_emb))
            .limit(3)
        )
        results = list((await session.scalars(statement)).all())
        print(f"\n[VERIFIED] Top 3 pgvector cosine matches for '{query}':")
        for r in results:
            print(f"  - [{r.source_name}] ({r.metadata_json.get('compliance', 'N/A')}): {r.content[:75]}...")

    await db.close()


if __name__ == "__main__":
    asyncio.run(seed_vector_database())
