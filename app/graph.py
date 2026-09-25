from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass
from typing import NamedTuple


class Node(NamedTuple):
    id: str
    x: float
    y: float
    label: str


WAREHOUSE_NODES: dict[str, Node] = {
    # Docks & Charging
    "DOCK-W": Node("DOCK-W", 92.0, 270.0, "DOCK WEST"),
    "DOCK-E": Node("DOCK-E", 908.0, 270.0, "DOCK EAST"),
    "CHARGE": Node("CHARGE", 500.0, 540.0, "CHARGE BAY"),

    # Main Corridors
    "INT-W1": Node("INT-W1", 280.0, 270.0, "JCT-W1"),
    "INT-W2": Node("INT-W2", 410.0, 270.0, "WP-04 (West Hold)"),
    "C-14": Node("C-14", 500.0, 270.0, "CORRIDOR C-14"),
    "INT-E1": Node("INT-E1", 590.0, 270.0, "WP-09 (East Hold)"),
    "INT-E2": Node("INT-E2", 720.0, 270.0, "JCT-E1"),

    # Vertical Spine
    "INT-N1": Node("INT-N1", 500.0, 85.0, "NORTH APEX"),
    "INT-N2": Node("INT-N2", 500.0, 180.0, "WP-02 (North Hold)"),
    "INT-S1": Node("INT-S1", 500.0, 415.0, "WP-07 (South Mid)"),
    "INT-S2": Node("INT-S2", 500.0, 485.0, "SOUTH SPINE"),

    # Bypass Highway & Aisle B-07
    "BYPASS-W": Node("BYPASS-W", 102.0, 415.0, "BYPASS WEST"),
    "BYPASS-WM": Node("BYPASS-WM", 280.0, 415.0, "BYPASS W-MID"),
    "AISLE-B07": Node("AISLE-B07", 640.0, 415.0, "AISLE B-07"),
    "BYPASS-EM": Node("BYPASS-EM", 720.0, 415.0, "BYPASS E-MID"),
    "BYPASS-E": Node("BYPASS-E", 900.0, 415.0, "BYPASS EAST"),

    # Perimeter Detour around B-07
    "DETOUR-SW": Node("DETOUR-SW", 280.0, 505.0, "DETOUR SW"),
    "DETOUR-SE": Node("DETOUR-SE", 720.0, 505.0, "DETOUR SE"),
    "DETOUR-S": Node("DETOUR-S", 500.0, 505.0, "DETOUR JCT"),  # where the detour lane crosses the south spine

    # Racks A-01 to C-04
    "RACK A-01": Node("RACK A-01", 210.0, 155.0, "RACK A-01 [BULK]"),
    "RACK A-02": Node("RACK A-02", 370.0, 155.0, "RACK A-02 [PARTS]"),
    "RACK A-03": Node("RACK A-03", 630.0, 155.0, "RACK A-03 [FAST]"),
    "RACK A-04": Node("RACK A-04", 790.0, 155.0, "RACK A-04 [RESERVE]"),

    "RACK B-01": Node("RACK B-01", 210.0, 355.0, "RACK B-01 [AVIONICS]"),
    "RACK B-02": Node("RACK B-02", 370.0, 355.0, "RACK B-02 [ASSEMBLY]"),
    "RACK B-03": Node("RACK B-03", 630.0, 355.0, "RACK B-03 [HARNESS]"),
    "RACK B-04": Node("RACK B-04", 790.0, 355.0, "RACK B-04 [OPTICS]"),

    "RACK C-01": Node("RACK C-01", 210.0, 485.0, "RACK C-01 [STAGING]"),
    "RACK C-02": Node("RACK C-02", 370.0, 485.0, "RACK C-02 [FINISHED]"),
    "RACK C-03": Node("RACK C-03", 630.0, 485.0, "RACK C-03 [BUFFER]"),
    "RACK C-04": Node("RACK C-04", 790.0, 485.0, "RACK C-04 [PACKAGING]"),
}

# Nodes whose entry requires a peer lease (single-lane choke points).
MUTEX_ZONES: frozenset[str] = frozenset({"C-14"})

CHARGE_NODE = "CHARGE"

_EDGES: list[tuple[str, str]] = [
    # Main East-West Corridor (y = 270)
    ("DOCK-W", "INT-W1"), ("INT-W1", "INT-W2"), ("INT-W2", "C-14"),
    ("C-14", "INT-E1"), ("INT-E1", "INT-E2"), ("INT-E2", "DOCK-E"),
    # Main North-South Spine (x = 500)
    ("INT-N1", "INT-N2"), ("INT-N2", "C-14"), ("C-14", "INT-S1"),
    ("INT-S1", "INT-S2"), ("INT-S2", "DETOUR-S"), ("DETOUR-S", "CHARGE"),
    # Lower Transit Highway (y = 415)
    ("BYPASS-W", "BYPASS-WM"), ("BYPASS-WM", "INT-S1"), ("INT-S1", "AISLE-B07"),
    ("AISLE-B07", "BYPASS-EM"), ("BYPASS-EM", "BYPASS-E"),
    # Vertical Interconnections
    ("INT-W1", "BYPASS-WM"), ("INT-E2", "BYPASS-EM"), ("DOCK-W", "BYPASS-W"), ("DOCK-E", "BYPASS-E"),
    # Detour Perimeter Highway around B-07
    ("BYPASS-WM", "DETOUR-SW"), ("DETOUR-SW", "DETOUR-S"), ("DETOUR-S", "DETOUR-SE"), ("DETOUR-SE", "BYPASS-EM"),
    # Rack Feeder Links
    ("RACK A-01", "INT-W1"), ("RACK A-02", "INT-W2"), ("RACK A-03", "INT-E1"), ("RACK A-04", "INT-E2"),
    ("RACK B-01", "INT-W1"), ("RACK B-02", "INT-W2"), ("RACK B-03", "AISLE-B07"), ("RACK B-04", "BYPASS-EM"),
    ("RACK C-01", "BYPASS-WM"), ("RACK C-02", "INT-S2"), ("RACK C-03", "DETOUR-SE"), ("RACK C-04", "BYPASS-E"),
]


def _build_adjacency() -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {k: [] for k in WAREHOUSE_NODES}
    for a, b in _EDGES:
        adj[a].append(b)
        adj[b].append(a)
    return adj


GRAPH_ADJACENCY = _build_adjacency()


@dataclass(frozen=True)
class RobotSpec:
    id: str
    name: str
    color: str
    max_payload: float
    speed: float  # world units per control tick
    home: str
    battery: float


# The fleet is data: add a robot here and it appears in state, bidding and the /api/world contract.
ROBOT_FLEET: tuple[RobotSpec, ...] = (
    RobotSpec("AMR-01", "Atlas", "#C2541A", 1200.0, 20.0, "DOCK-W", 96.0),
    RobotSpec("AMR-02", "Nova", "#F59E0B", 350.0, 24.0, "INT-N1", 90.0),
    RobotSpec("AMR-03", "Kite", "#38BDF8", 700.0, 20.0, "DOCK-E", 94.0),
)


def node_kind(node_id: str) -> str:
    if node_id.startswith("DOCK"):
        return "dock"
    if node_id == CHARGE_NODE:
        return "charge"
    if node_id.startswith("RACK"):
        return "rack"
    return "corridor" if node_id in MUTEX_ZONES else "transit"


def world_payload(fleet: tuple[RobotSpec, ...] = ROBOT_FLEET) -> dict:
    return {
        "nodes": [{"id": n.id, "x": n.x, "y": n.y, "label": n.label, "type": node_kind(n.id)} for n in WAREHOUSE_NODES.values()],
        "edges": [[a, b] for a, b in _EDGES],
        "mutex_zones": sorted(MUTEX_ZONES),
        "robots": [
            {"id": r.id, "name": r.name, "color": r.color, "max_payload_kg": r.max_payload, "speed": r.speed, "home": r.home}
            for r in fleet
        ],
    }


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def resolve_node(ref: str) -> str | None:
    """Map a node id, label or short alias ('B-07', 'Dock E') to a canonical node id."""
    if ref in WAREHOUSE_NODES:
        return ref
    key = _norm(ref)
    if not key:
        return None
    for node in WAREHOUSE_NODES.values():
        if key in (_norm(node.id), _norm(node.label)):
            return node.id
    suffix = [n.id for n in WAREHOUSE_NODES.values() if _norm(n.id).endswith(key) and not n.id.startswith("RACK")]
    return suffix[0] if len(suffix) == 1 else None


def find_shortest_path(start_id: str, goal_id: str, blocked_nodes: set[str] | frozenset[str] | None = None) -> list[tuple[float, float]] | None:
    """A* over the warehouse graph. Returns None when the goal is unreachable without entering blocked nodes."""
    blocked = blocked_nodes or ()
    ids = find_shortest_node_path(start_id, goal_id, blocked)
    return None if ids is None else [(WAREHOUSE_NODES[i].x, WAREHOUSE_NODES[i].y) for i in ids]


def find_shortest_node_path(start_id: str, goal_id: str, blocked: set[str] | frozenset[str] | tuple = ()) -> list[str] | None:
    start = WAREHOUSE_NODES[start_id]
    goal = WAREHOUSE_NODES[goal_id]
    if start.id == goal.id:
        return [start.id]
    if goal.id in blocked:
        return None

    frontier: list[tuple[float, str]] = [(0.0, start.id)]
    came_from: dict[str, str] = {}
    cost_so_far: dict[str, float] = {start.id: 0.0}

    while frontier:
        _, current = heapq.heappop(frontier)
        if current == goal.id:
            path = [current]
            while path[-1] in came_from:
                path.append(came_from[path[-1]])
            path.reverse()
            return path
        here = WAREHOUSE_NODES[current]
        for neighbor_id in GRAPH_ADJACENCY[current]:
            if neighbor_id in blocked:
                continue
            neighbor = WAREHOUSE_NODES[neighbor_id]
            new_cost = cost_so_far[current] + math.hypot(neighbor.x - here.x, neighbor.y - here.y)
            if new_cost < cost_so_far.get(neighbor_id, math.inf):
                cost_so_far[neighbor_id] = new_cost
                heapq.heappush(frontier, (new_cost + math.hypot(goal.x - neighbor.x, goal.y - neighbor.y), neighbor_id))
                came_from[neighbor_id] = current
    return None


def path_length(points: list[tuple[float, float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def find_closest_node(x: float, y: float) -> str:
    return min(WAREHOUSE_NODES.values(), key=lambda n: math.hypot(n.x - x, n.y - y)).id
