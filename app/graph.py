from __future__ import annotations

import heapq
import math
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
    "INT-W2": Node("INT-W2", 410.0, 270.0, "WP-04"),
    "C-14": Node("C-14", 500.0, 270.0, "CORRIDOR C-14"),
    "INT-E1": Node("INT-E1", 590.0, 270.0, "WP-09"),
    "INT-E2": Node("INT-E2", 720.0, 270.0, "JCT-E1"),

    # Vertical Spine
    "INT-N1": Node("INT-N1", 500.0, 85.0, "NORTH APEX"),
    "INT-N2": Node("INT-N2", 500.0, 180.0, "WP-02"),
    "INT-S1": Node("INT-S1", 500.0, 415.0, "WP-07"),
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

    # Racks A-01 to C-04
    "RACK A-01": Node("RACK A-01", 210.0, 155.0, "RACK A-01"),
    "RACK A-02": Node("RACK A-02", 370.0, 155.0, "RACK A-02"),
    "RACK A-03": Node("RACK A-03", 630.0, 155.0, "RACK A-03"),
    "RACK A-04": Node("RACK A-04", 790.0, 155.0, "RACK A-04"),

    "RACK B-01": Node("RACK B-01", 210.0, 355.0, "RACK B-01"),
    "RACK B-02": Node("RACK B-02", 370.0, 355.0, "RACK B-02"),
    "RACK B-03": Node("RACK B-03", 630.0, 355.0, "RACK B-03"),
    "RACK B-04": Node("RACK B-04", 790.0, 355.0, "RACK B-04"),

    "RACK C-01": Node("RACK C-01", 210.0, 485.0, "RACK C-01"),
    "RACK C-02": Node("RACK C-02", 370.0, 485.0, "RACK C-02"),
    "RACK C-03": Node("RACK C-03", 630.0, 485.0, "RACK C-03"),
    "RACK C-04": Node("RACK C-04", 790.0, 485.0, "RACK C-04"),
}


def _build_adjacency() -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {k: [] for k in WAREHOUSE_NODES}

    def connect(a: str, b: str) -> None:
        if a in adj and b in adj:
            if b not in adj[a]:
                adj[a].append(b)
            if a not in adj[b]:
                adj[b].append(a)

    # Main East-West Corridor (y = 270)
    connect("DOCK-W", "INT-W1")
    connect("INT-W1", "INT-W2")
    connect("INT-W2", "C-14")
    connect("C-14", "INT-E1")
    connect("INT-E1", "INT-E2")
    connect("INT-E2", "DOCK-E")

    # Main North-South Spine (x = 500)
    connect("INT-N1", "INT-N2")
    connect("INT-N2", "C-14")
    connect("C-14", "INT-S1")
    connect("INT-S1", "INT-S2")
    connect("INT-S2", "CHARGE")

    # Lower Transit Highway (y = 415)
    connect("BYPASS-W", "BYPASS-WM")
    connect("BYPASS-WM", "INT-S1")
    connect("INT-S1", "AISLE-B07")
    connect("AISLE-B07", "BYPASS-EM")
    connect("BYPASS-EM", "BYPASS-E")

    # Vertical Interconnections
    connect("INT-W1", "BYPASS-WM")
    connect("INT-E2", "BYPASS-EM")
    connect("DOCK-W", "BYPASS-W")
    connect("DOCK-E", "BYPASS-E")

    # Detour Perimeter Highway around B-07
    connect("BYPASS-WM", "DETOUR-SW")
    connect("DETOUR-SW", "DETOUR-SE")
    connect("DETOUR-SE", "BYPASS-EM")

    # Rack Feeder Links
    connect("RACK A-01", "INT-W1")
    connect("RACK A-02", "INT-W2")
    connect("RACK A-03", "INT-E1")
    connect("RACK A-04", "INT-E2")

    connect("RACK B-01", "INT-W1")
    connect("RACK B-02", "INT-W2")
    connect("RACK B-03", "AISLE-B07")
    connect("RACK B-04", "BYPASS-EM")

    connect("RACK C-01", "BYPASS-WM")
    connect("RACK C-02", "INT-S2")
    connect("RACK C-03", "DETOUR-SE")
    connect("RACK C-04", "BYPASS-E")

    return adj


GRAPH_ADJACENCY = _build_adjacency()


def find_shortest_path(start_id: str, goal_id: str, blocked_nodes: set[str] | None = None) -> list[tuple[float, float]]:
    """Real A* shortest path search on warehouse graph."""
    blocked = blocked_nodes or set()
    start_node = WAREHOUSE_NODES.get(start_id, WAREHOUSE_NODES["DOCK-W"])
    goal_node = WAREHOUSE_NODES.get(goal_id, WAREHOUSE_NODES["DOCK-E"])

    if start_node.id == goal_node.id:
        return [(start_node.x, start_node.y)]

    frontier: list[tuple[float, str]] = []
    heapq.heappush(frontier, (0.0, start_node.id))
    came_from: dict[str, str] = {}
    cost_so_far: dict[str, float] = {start_node.id: 0.0}

    while frontier:
        _, current = heapq.heappop(frontier)

        if current == goal_node.id:
            # Reconstruct path
            path: list[tuple[float, float]] = []
            curr_id: str | None = current
            while curr_id:
                node = WAREHOUSE_NODES[curr_id]
                path.append((node.x, node.y))
                curr_id = came_from.get(curr_id)
            path.reverse()
            return path

        curr_node = WAREHOUSE_NODES[current]
        for neighbor_id in GRAPH_ADJACENCY.get(current, []):
            if neighbor_id in blocked:
                continue
            neighbor = WAREHOUSE_NODES[neighbor_id]
            edge_weight = math.hypot(neighbor.x - curr_node.x, neighbor.y - curr_node.y)
            new_cost = cost_so_far[current] + edge_weight

            if neighbor_id not in cost_so_far or new_cost < cost_so_far[neighbor_id]:
                cost_so_far[neighbor_id] = new_cost
                priority = new_cost + math.hypot(goal_node.x - neighbor.x, goal_node.y - neighbor.y)
                heapq.heappush(frontier, (priority, neighbor_id))
                came_from[neighbor_id] = current

    # Direct fallback if disconnected
    return [(start_node.x, start_node.y), (goal_node.x, goal_node.y)]


def find_closest_node(x: float, y: float) -> str:
    closest_id = "DOCK-W"
    min_dist = float("inf")
    for node_id, node in WAREHOUSE_NODES.items():
        d = math.hypot(node.x - x, node.y - y)
        if d < min_dist:
            min_dist = d
            closest_id = node_id
    return closest_id
