from __future__ import annotations

import hashlib
import math
import re

DIM = 384
_STOPWORDS = frozenset("a an and are as at be by for from in is it of on or the to with when if than then that this".split())

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
        "Low Battery Hand-off and Autonomous Docking: Robots reaching state-of-charge <= 20% SoC enter high-priority dock reservation at the charge bay. Active pick-and-carry tasks are automatically handed off to the nearest idle AMR with SoC > 60%.",
        {"category": "POWER_MANAGEMENT", "zone": "CHARGE", "compliance": "INTERNAL_SOP"},
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


def text_to_embedding(text: str, dim: int = DIM) -> list[float]:
    """Deterministic feature-hashed bag-of-words vector (L2-normalised).

    Texts sharing vocabulary get high cosine similarity; stable across processes (sha256, not hash()).
    ponytail: lexical, not semantic — swap for a real embedding model if paraphrase recall matters.
    """
    vec = [0.0] * dim
    tokens = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        vec[int.from_bytes(digest[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [x / norm for x in vec]
