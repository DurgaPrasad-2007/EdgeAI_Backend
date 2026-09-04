from __future__ import annotations

import asyncio
import math
import random
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
from app.settings import Settings


def text_to_embedding(text: str, dim: int = 384) -> list[float]:
    """Deterministic normalized 384-d vector embedding for local edge retrieval."""
    random.seed(hash(text.strip().lower()) & 0xFFFFFFFF)
    vec = [random.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


WAREHOUSE_SOPS = [
    (
        "SOP-AMR-001",
        "Corridor C-14 Choke Point Arbitration: Single-lane corridor C-14 entry is strictly governed by peer lease negotiation. When two AMRs arrive at the intersection, tie-breaking follows: Safety Envelope > Task Urgency > State-of-Charge (SoC) > Monotonic Arrival Timestamp. Yielding robots must hold at WP-04.",
        {"category": "CORRIDOR_ARBITRATION", "zone": "C-14", "compliance": "ISO-3691-4"},
    ),
    (
        "SOP-AMR-002",
        "Aisle B-07 Obstacle / Spill Emergency Detour: If LiDAR / 3D Time-of-Flight sensors detect a stationary obstacle (>3000ms) in Aisle B-07, robot enters STOP_AND_REROUTE mode, broadcasts BLOCKAGE_DETECTED on local DDS mesh, and re-routes via Perimeter Lane P-2. Current payload task is re-bid among peers.",
        {"category": "INCIDENT_DETOUR", "zone": "B-07", "compliance": "IEC-62443"},
    ),
    (
        "SOP-AMR-003",
        "Low Battery Hand-off and Autonomous Docking: Robots reaching state-of-charge <= 20% SoC enter high-priority dock reservation at Dock-West Bay 3. Active pick-and-carry tasks are automatically handed off to the nearest idle AMR with SoC > 60%.",
        {"category": "POWER_MANAGEMENT", "zone": "DOCK_WEST", "compliance": "INTERNAL_SOP"},
    ),
    (
        "SOP-AMR-004",
        "Zero-Motion Safety Boundary and Audit Ingestion: The backend API and PostgreSQL database serve exclusively as telemetry ingestion and RAG audit stores. Under zero circumstances shall the cloud or dashboard issue raw motor/velocity drive packets; motion execution remains strictly onboard the AMR microcontroller.",
        {"category": "SAFETY_GOVERNANCE", "zone": "GLOBAL", "compliance": "BEL_SIH26123"},
    ),
    (
        "SOP-AMR-005",
        "Heterogeneous Fleet Adapter Protocol: AMRs exchange position, intent, and corridor leases using lightweight JSON payload contracts over local peer network. Unresponsive peers (>1500ms heartbeat loss) are treated as virtual physical obstacles with a 1.2m conservative safety zone.",
        {"category": "MESH_PROTOCOL", "zone": "GLOBAL", "compliance": "OPEN_RMF_COMPAT"},
    ),
]


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
