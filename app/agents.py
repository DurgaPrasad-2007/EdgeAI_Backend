"""Decentralised AMR agents and the peer-to-peer mesh they talk over.

Every robot is a `RobotAgent` with private state (pose, path, battery, task) and a private, *soft-state* view
of its peers built only from the messages it has heard. There is no shared lock and no shared reservation
table: a robot enters a node only if no live peer has claimed it, a single-lane corridor (mutex zone) is
"leased" by claiming it on the mesh, and every claim expires if its owner falls silent (heartbeat timeout).
Tasks are allocated by a Contract-Net auction that the robots run among themselves.

`PeerBus` is an in-process transport with the semantics of a pub/sub mesh (Zenoh/DDS style): broadcast or
directed messages, a per-robot inbox, and a radio-silence switch. Swap it for a network transport and the
agents do not change. `World` is ground-truth physics (positions, obstacles, collision counting): robots
write only their own pose and read it only through sensing, never other robots' internal state.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from app.graph import (
    CHARGE_NODE,
    GRAPH_ADJACENCY,
    MUTEX_ZONES,
    WAREHOUSE_NODES,
    RobotSpec,
    find_closest_node,
    find_shortest_node_path,
    path_length,
)
from app.schemas import Point, RobotState

log = logging.getLogger("edgefleet.agents")

CONTROL_PERIOD_SECONDS = 0.6
LOW_BATTERY = 30.0          # idle robots below this go to the charge bay
MIN_BID_BATTERY = 20.0      # robots below this may not bid
CHARGED = 95.0
CHARGE_RATE = 1.5           # % per tick while docked at the charge bay
DRAIN_PER_TICK = 0.08       # % per tick while moving
LEASE_TICKS = 8             # a claim is soft state: it expires this many ticks after its owner goes silent
HEARTBEAT_TIMEOUT = LEASE_TICKS
HEARTBEAT_EVERY = 8
DEADLOCK_TICKS = 12         # ticks a robot waits before it tries a detour / side-step
HOLD_TICKS = 15             # a robot that stepped aside stays put this long so the other can pass
LOOKAHEAD_NODES = 2         # nodes a robot must claim (all-or-nothing) before it moves
SOFT_DETOUR_FACTOR = 1.35   # congestion-aware routing accepts a route up to this much longer than the shortest
INTENT_HORIZON = 3          # how far ahead a robot advertises the mutex zone it is heading for
COLLISION_RADIUS = 10.0
BASE_PRIORITY = 50
AUCTION_TICKS = 3           # an auction closes early if a peer stays silent this long
REANNOUNCE_TICKS = 4        # an unawarded task is announced again after this long
PATIENCE_TICKS = 3          # decentralised: after this many blocked ticks a robot weighs a detour against waiting
DETOUR_COOLDOWN_TICKS = 20  # ...but never more often than this, so a robot cannot flip-flop between two routes
DETOUR_BUDGET_TICKS = 14    # ...and takes it if the extra travel costs less than this many ticks
SW_TIMEOUT_TICKS = 40       # stop-and-wait baseline: how long it waits before trying a detour
DROPOUT_TICKS = 30          # a simulated radio blackout lasts this long
POLICIES = ("decentralized", "stop_and_wait")

_COORD_NODE = {(round(n.x), round(n.y)): n.id for n in WAREHOUSE_NODES.values()}


def node_at(point: Point) -> str | None:
    return _COORD_NODE.get((round(point.x), round(point.y)))


def points(node_ids: list[str]) -> list[Point]:
    return [Point(x=WAREHOUSE_NODES[i].x, y=WAREHOUSE_NODES[i].y) for i in node_ids]


def _xy(pts: list[Point]) -> list[tuple[float, float]]:
    return [(p.x, p.y) for p in pts]


def label(node_id: str) -> str:
    return WAREHOUSE_NODES[node_id].label


class Clock:
    """Shared time base (the fleet's NTP). Manual in tests/benchmarks, advanced by a ticker when live."""

    def __init__(self) -> None:
        self.tick = 0


@dataclass(slots=True)
class Msg:
    type: str
    sender: str
    to: str
    body: dict[str, Any]
    tick: int
    seq: int = 0


class PeerBus:
    """In-process pub/sub mesh: per-robot inboxes, broadcast/directed delivery, radio silence, observer taps."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.inboxes: dict[str, deque[Msg]] = {}
        self.handlers: dict[str, Callable[[], bool]] = {}
        self.wakers: dict[str, asyncio.Event] = {}
        self.taps: list[Callable[[Msg], None]] = []
        self.event_sinks: list[Callable[[str, str], None]] = []
        self.silenced: set[str] = set()
        self._seq = 0

    def register(self, robot_id: str, handler: Callable[[], bool]) -> None:
        self.inboxes[robot_id] = deque()
        self.handlers[robot_id] = handler

    def waker(self, robot_id: str) -> asyncio.Event:
        return self.wakers.setdefault(robot_id, asyncio.Event())

    def publish(self, msg: Msg) -> None:
        if msg.sender in self.silenced:
            return  # radio down: nothing leaves the robot
        self._seq += 1
        msg.seq = self._seq
        for tap in self.taps:
            tap(msg)
        for rid, box in self.inboxes.items():
            if rid == msg.sender or rid in self.silenced or msg.to not in ("MESH", rid):
                continue
            box.append(msg)
            if rid in self.wakers:
                self.wakers[rid].set()

    def log(self, type_: str, text: str) -> None:
        for sink in self.event_sinks:
            sink(type_, text)

    def pump(self, limit: int = 2000) -> None:
        """Deliver every queued message (used by the manual clock, and to settle after an API call)."""
        for _ in range(limit):
            progressed = False
            for handler in tuple(self.handlers.values()):
                progressed = handler() or progressed
            if not progressed:
                return


class World:
    """Ground truth: where robots physically are, which aisles are blocked, and whether robots touch."""

    def __init__(self, fleet: tuple[RobotSpec, ...]) -> None:
        self.pos: dict[str, tuple[float, float]] = {}
        self.docked: dict[str, bool] = {}
        self.parked: dict[str, bool] = {}  # standing in a dock/home/charge spot without claiming a lane node
        self.obstacles: set[str] = set()
        self.collisions = 0
        self.staging = {spec.home for spec in fleet} | {CHARGE_NODE} | {n for n in WAREHOUSE_NODES if n.startswith("DOCK")}
        self._touching: set[frozenset[str]] = set()
        for spec in fleet:
            node = WAREHOUSE_NODES[spec.home]
            self.pos[spec.id] = (node.x, node.y)
            self.docked[spec.id] = True
            self.parked[spec.id] = True

    def nearest(self, x: float, y: float) -> str:
        return min(self.pos, key=lambda rid: math.hypot(self.pos[rid][0] - x, self.pos[rid][1] - y))

    def check_collisions(self, log: Callable[[str, str], None]) -> None:
        active = [rid for rid in self.pos if not self.docked.get(rid)]
        touching = set()
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if math.hypot(self.pos[a][0] - self.pos[b][0], self.pos[a][1] - self.pos[b][1]) < COLLISION_RADIUS:
                    if self.parked.get(a) or self.parked.get(b):
                        continue  # docks, home spots and the charge bay are staging areas: robots idle side by side
                    touching.add(frozenset((a, b)))
        for pair in touching - self._touching:
            self.collisions += 1
            log("REROUTE", f"Proximity violation: {' & '.join(sorted(pair))}")
        self._touching = touching


@dataclass(slots=True)
class Peer:
    """What one robot knows about another, from the messages it has heard (never from shared memory)."""

    id: str
    last_seen: int = -10_000
    node: str | None = None
    x: float = 0.0
    y: float = 0.0
    claims: frozenset[str] = frozenset()
    occupied: tuple[str, ...] = ()
    status: str = "Idle"
    battery: float = 100.0
    priority: int = BASE_PRIORITY
    wait_for: str | None = None
    wait_node: str | None = None
    path_nodes: tuple[str, ...] = ()
    intent_zone: str | None = None
    intent_hops: int = 99
    docked: bool = True
    dead_reported: bool = False

    @property
    def rank(self) -> tuple[float, str]:
        return (self.priority + (100.0 - self.battery) * 0.2, self.id)


@dataclass(slots=True)
class Auction:
    task: dict[str, Any]
    started: int
    bids: dict[str, float | None] = field(default_factory=dict)
    only: list[str] | None = None
    closed: bool = False


class RobotAgent:
    def __init__(self, spec: RobotSpec, bus: PeerBus, world: World, clock: Clock, policy: str = "decentralized") -> None:
        self.spec, self.id = spec, spec.id
        self.bus, self.world, self.clock = bus, world, clock
        # Baseline = classic stop-and-wait: same safety reservations, but a robot that meets a conflict simply stops
        # until the path clears. No intent-based right-of-way, no congestion-aware routing, only a slow timeout
        # (SW_TIMEOUT_TICKS) before it tries a detour or side-step.
        self.sw = policy == "stop_and_wait"
        self.lookahead = LOOKAHEAD_NODES
        self.deadlock_ticks = SW_TIMEOUT_TICKS if self.sw else DEADLOCK_TICKS

        home = WAREHOUSE_NODES[spec.home]
        self.pos = Point(x=home.x, y=home.y)
        self.path = [self.pos.model_copy()]
        self.path_index, self.progress = 0, 0.0
        self.battery = spec.battery
        self.status = "Idle"
        self.task_text = f"Standby at {home.label}"
        self.task: dict[str, Any] | None = None
        self.leg, self.target = "idle", None
        self.priority = BASE_PRIORITY
        self.completed = 0
        self.payload = 0.0
        self.decision = "Docked at home; listening on the mesh"

        self.peers: dict[str, Peer] = {}
        self.blocked: set[str] = set()
        self.claims: set[str] = set()
        self.manual_leases: dict[str, int] = {}
        self.leased: set[str] = set()
        self.waiting = 0
        self.stuck = 0            # ticks blocked since the robot last actually moved (drives deadlock handling)
        self.last_detour = -10_000
        self.wait_for: str | None = None
        self.wait_node: str | None = None
        self.hold_until = 0
        self.auctions: dict[str, Auction] = {}
        self.owners: dict[str, str] = {}      # task id -> robot that won it (learned from AWARD messages)
        self.online = True
        self.offline_until = 0
        self.seq = 0

        bus.register(self.id, self.handle_inbox)

    # ── messaging ────────────────────────────────────────────────────────

    def say(self, type_: str, body: dict[str, Any], to: str = "MESH") -> None:
        self.bus.publish(Msg(type_, self.id, to, body, self.clock.tick))

    def log(self, type_: str, text: str) -> None:
        if self.online:
            self.bus.log(type_, text)

    def handle_inbox(self) -> bool:
        box = self.bus.inboxes[self.id]
        if not box or not self.online:
            box.clear()
            return False
        while box:
            try:
                self._on(box.popleft())
            except Exception:  # one malformed/unexpected message must never take a robot off the mesh
                log.exception("%s failed to handle a mesh message", self.id)
        return True

    def _on(self, msg: Msg) -> None:
        handler = {
            "POSE": self._on_pose,
            "OBSTACLE_ALERT": self._on_obstacle,
            "TASK_ANNOUNCE": self._on_announce,
            "TASK_BID": self._on_bid,
            "TASK_AWARD": self._on_award,
            "TASK_REVOKE": self._on_revoke,
            "TASK_DIRECTIVE": self._on_directive,
            "TASK_UPDATE": self._on_update,
            "TASK_STATUS": self._on_status,
            "ORPHAN": self._on_status,
            "SYNC_REQ": self._on_sync_req,
            "SYNC": self._on_sync,
        }.get(msg.type)
        if handler:
            handler(msg)

    # ── peer table ───────────────────────────────────────────────────────

    def _alive(self, peer: Peer) -> bool:
        return self.clock.tick - peer.last_seen <= HEARTBEAT_TIMEOUT

    def _live_peers(self) -> list[Peer]:
        return [p for _, p in sorted(self.peers.items()) if self._alive(p)]

    def _on_pose(self, msg: Msg) -> None:
        b = msg.body
        peer = self.peers.setdefault(msg.sender, Peer(msg.sender))
        if peer.dead_reported:
            peer.dead_reported = False
            self.log("HEARTBEAT", f"{msg.sender} rejoined the mesh")
        peer.last_seen = msg.tick
        peer.node, peer.x, peer.y = b["node"], b["x"], b["y"]
        peer.claims = frozenset(b["claims"])
        peer.occupied = tuple(b["occupied"])
        peer.status, peer.battery, peer.priority = b["status"], b["battery"], b["priority"]
        peer.wait_for, peer.wait_node = b["wait_for"], b["wait_node"]
        peer.path_nodes = tuple(b["path_nodes"])
        peer.intent_zone, peer.intent_hops = b["intent_zone"], b["intent_hops"]
        peer.docked = b["docked"]

    def _expire_peers(self) -> None:
        """Heartbeat loss: a silent peer's claims lapse (bounded lease) and its task is handed back to the mesh."""
        for peer in self.peers.values():
            if peer.dead_reported or self._alive(peer):
                continue
            peer.dead_reported = True
            self.log("HEARTBEAT", f"{self.id} lost heartbeat from {peer.id}; its claims expire after {LEASE_TICKS * CONTROL_PERIOD_SECONDS:.1f}s")
            if self.wait_for == peer.id:
                self.wait_for = None
            live_ids = sorted([self.id] + [p.id for p in self._live_peers()])
            if live_ids[0] == self.id:  # the lowest-id survivor speaks for the mesh
                for task_id, owner in list(self.owners.items()):
                    if owner == peer.id:
                        self.say("ORPHAN", {"task_id": task_id, "robot": peer.id})
                        del self.owners[task_id]

    def _ghosts(self) -> set[str]:
        """Nodes physically occupied by peers that went silent: they cannot be entered or routed through."""
        return {n for p in self.peers.values() if not self._alive(p) and not p.docked for n in p.occupied}

    def _claimant(self, node: str | None, exclude: str | None = None) -> str | None:
        if node is None:
            return None
        for peer in self._live_peers():
            if peer.id != exclude and not peer.docked and node in peer.claims:
                return peer.id
        return None

    def _all_claimed(self) -> set[str]:
        nodes = set(self.claims)
        for peer in self._live_peers():
            if not peer.docked:
                nodes |= peer.claims
        return nodes

    def _rank(self) -> tuple[float, str]:
        return (self.priority + (100.0 - self.battery) * 0.2, self.id)

    # ── task allocation (decentralised Contract-Net) ─────────────────────

    def is_free(self) -> bool:
        return self.online and self.task is None and self.battery >= MIN_BID_BATTERY and self.status != "Blocked"

    def _bid(self, task: dict[str, Any]) -> float | None:
        if task["payload_kg"] > self.spec.max_payload:
            return None
        route = self._plan(task["pickup"])
        if route is None:
            return None
        distance = path_length(_xy(route))
        return (self.spec.max_payload - task["payload_kg"]) * 0.05 + self.battery * 0.40 - distance * 0.08 + task["priority"] * 0.25

    def can_take(self, task: dict[str, Any]) -> bool:
        return self.is_free() and self._bid(task) is not None

    def _on_announce(self, msg: Msg) -> None:
        task = msg.body["task"]
        if task["id"] in self.owners and msg.body.get("force") is not True:
            return
        only = msg.body.get("only")
        auction = self.auctions.setdefault(task["id"], Auction(task, self.clock.tick, only=only))
        auction.task, auction.started, auction.closed, auction.only = task, self.clock.tick, False, only
        score = self._bid(task) if self.is_free() and (only is None or self.id in only) else None
        auction.bids[self.id] = score
        self.say("TASK_BID", {"task_id": task["id"], "score": score})
        self._try_close(task["id"])

    def _on_bid(self, msg: Msg) -> None:
        task_id = msg.body["task_id"]
        auction = self.auctions.get(task_id)
        if auction is None:
            return
        auction.bids[msg.sender] = msg.body["score"]
        self._try_close(task_id)

    def _try_close(self, task_id: str, force: bool = False) -> None:
        auction = self.auctions.get(task_id)
        if auction is None or auction.closed:
            return
        expected = {self.id} | {p.id for p in self._live_peers()}
        if not force and not expected <= set(auction.bids):
            return
        auction.closed = True
        bids = [(score, rid) for rid, score in auction.bids.items() if score is not None and rid not in self._busy_peers()]
        if not bids:
            return
        score, winner = max(bids)
        if winner == self.id and self.is_free():
            self._take(auction.task, score)
            self.say("TASK_AWARD", {"task_id": task_id, "robot": self.id, "score": round(score, 1), "bids": {k: v for k, v in auction.bids.items() if v is not None}})

    def _busy_peers(self) -> set[str]:
        return set(self.owners.values())

    def _on_award(self, msg: Msg) -> None:
        task_id, winner = msg.body["task_id"], msg.body["robot"]
        self.owners[task_id] = winner
        self.auctions.pop(task_id, None)
        if self.task and self.task["id"] == task_id and winner != self.id and self._rank()[1] > winner:
            self._free()  # two robots both thought they won: the lower id keeps it
            self.decision = f"Released {task_id}: {winner} won the same auction"

    def _on_status(self, msg: Msg) -> None:
        if msg.type == "ORPHAN":
            self.owners.pop(msg.body["task_id"], None)
            self.auctions.pop(msg.body["task_id"], None)
        elif msg.body.get("status") in ("Completed", "Released"):
            self.owners.pop(msg.body["task_id"], None)

    def _on_revoke(self, msg: Msg) -> None:
        self.owners.pop(msg.body["task_id"], None)
        self.auctions.pop(msg.body["task_id"], None)
        if msg.body.get("robot") in (None, self.id) and self.task and self.task["id"] == msg.body["task_id"]:
            self.decision = f"Operator withdrew {msg.body['task_id']}"
            self._free()

    def _on_directive(self, msg: Msg) -> None:
        if msg.body["robot"] != self.id:
            return
        task = msg.body["task"]
        if self.can_take(task):
            self._take(task, 0.0)
            self.say("TASK_AWARD", {"task_id": task["id"], "robot": self.id, "score": 0.0, "bids": {}, "directive": True})

    def _on_update(self, msg: Msg) -> None:
        if self.task and self.task["id"] == msg.body["task_id"]:
            self.task.update({k: v for k, v in msg.body.items() if k in ("priority", "payload_kg", "payload_size", "urgency")})
            self.priority = self.task["priority"]

    def _take(self, task: dict[str, Any], score: float) -> None:
        self.owners[task["id"]] = self.id
        self.auctions.pop(task["id"], None)
        self.task = dict(task)
        self.leg, self.target = "to_pickup", task["pickup"]
        self.priority, self.payload = task["priority"], 0.0
        self.task_text = f"{task['id']}: {task['pickup']} → {task['destination']} ({task['payload_kg']:g}kg)"
        self.status = "Task handoff"
        self.waiting, self.stuck, self.wait_for = 0, 0, None
        self.decision = f"Won {task['id']} in the auction (score {score:.1f}); heading to {label(task['pickup'])}"
        self._route_leg()

    def _close_stale_auctions(self) -> None:
        for task_id, auction in list(self.auctions.items()):
            if not auction.closed and self.clock.tick - auction.started >= AUCTION_TICKS:
                self._try_close(task_id, force=True)
            elif auction.closed and self.clock.tick - auction.started > REANNOUNCE_TICKS * 2:
                del self.auctions[task_id]

    # ── obstacles ────────────────────────────────────────────────────────

    def detect_obstacle(self, node: str, blocked: bool) -> None:
        """This robot's sensors report an aisle blockage/clearance: tell the mesh and react locally."""
        self.say("OBSTACLE_ALERT", {"node": node, "blocked": blocked})
        self._apply_obstacle(node, blocked)

    def _on_obstacle(self, msg: Msg) -> None:
        self._apply_obstacle(msg.body["node"], msg.body["blocked"])

    def _apply_obstacle(self, node: str, blocked: bool) -> None:
        if (node in self.blocked) == blocked:
            return
        (self.blocked.add if blocked else self.blocked.discard)(node)
        if self.leg == "idle":
            return
        if blocked:
            if node in [node_at(p) for p in self.path[self.path_index + 1:]]:
                self._replan(force=True)
        else:
            self._replan(force=False)

    def _on_sync_req(self, msg: Msg) -> None:
        self.say("SYNC", {"blocked": sorted(self.blocked)}, to=msg.sender)

    def _on_sync(self, msg: Msg) -> None:
        self.blocked = set(msg.body["blocked"])
        if self.leg != "idle":
            self._replan(force=False)

    # ── routing ──────────────────────────────────────────────────────────

    def _plan(self, target: str, avoid: frozenset[str] | set[str] = frozenset(), soft: bool = False) -> list[Point] | None:
        """Route from the robot's physical position to `target`, never entering blocked nodes."""
        blocked = self.blocked | set(avoid) | self._ghosts()
        if soft and not self.sw:
            crowded = {n for n in self._all_claimed() if n not in self.claims and n != target}
            crowded -= {node_at(self.pos)}
            base = self._plan(target, avoid)
            alt = self._plan(target, set(avoid) | crowded) if crowded else None
            if base is not None and alt is not None and path_length(_xy(alt)) <= path_length(_xy(base)) * SOFT_DETOUR_FACTOR:
                return alt
            return base
        here = node_at(self.pos) if self.progress == 0.0 else None
        if here is not None:
            ids = find_shortest_node_path(here, target, blocked - {here})
            return None if ids is None else points(ids)
        best: tuple[float, list[Point]] | None = None
        for idx in dict.fromkeys((self.path_index, self.path_index + 1)):
            if not 0 <= idx < len(self.path):
                continue
            node = node_at(self.path[idx])
            if node is None or node in blocked:
                continue
            ids = find_shortest_node_path(node, target, blocked)
            if ids is None:
                continue
            route = points(ids)
            cost = math.hypot(self.path[idx].x - self.pos.x, self.path[idx].y - self.pos.y) + path_length(_xy(route))
            if best is None or cost < best[0]:
                best = (cost, route)
        return None if best is None else [self.pos.model_copy()] + best[1]

    def _apply_path(self, route: list[Point]) -> None:
        mid_edge = self.progress > 0.0 or node_at(self.pos) is None
        ahead = self.path_index + 1
        retained = node_at(self.path[ahead]) if mid_edge and ahead < len(self.path) else None
        self.path, self.path_index, self.progress = route, 0, 0.0
        self._prune(extra={retained})

    def _prune(self, extra: set[str | None] | frozenset = frozenset()) -> None:
        """A robot claims its node plus the (up to) `lookahead` path nodes it has reserved ahead."""
        window = {node_at(self.pos), *extra}
        window.update(node_at(p) for p in self.path[self.path_index + 1: self.path_index + 1 + self.lookahead])
        window.discard(None)
        self.claims &= window

    def _route_leg(self, announce: bool = True) -> bool:
        route = self._plan(self.target, soft=True) if self.target else None
        if route is None:
            if self.status != "Blocked" and announce:
                self.log("REROUTE", f"{self.id} has no route to {self.target}; holding until the path clears")
            self.status = "Blocked"
            self.decision = f"No route to {self.target}; retrying every 3 ticks"
            return False
        self._apply_path(route)
        if self.status == "Blocked":
            self.status = "Moving"
        return True

    def _replan(self, force: bool) -> None:
        if not self.target:
            return
        route = self._plan(self.target, soft=True)
        if route is None:
            self._route_leg()
            return
        old = [node_at(p) for p in self.path[self.path_index + 1:]]
        new = [n for n in (node_at(p) for p in route) if n]
        if self.progress == 0.0 and new and new[0] == node_at(self.pos):
            new = new[1:]
        if force or new != old:
            self._apply_path(route)
            self.status = "Rerouting"
            self.decision = f"Replanned A* route to {label(self.target)}"
            self.log("REROUTE", f"{self.id} replanned A* route to {label(self.target)}")

    def _send_idle(self) -> None:
        """Idle policy: charge when low, otherwise wait at the home dock."""
        at = node_at(self.pos) if self.progress == 0.0 else None
        home = self.spec.home
        want = CHARGE_NODE if (self.battery < LOW_BATTERY or (at == CHARGE_NODE and self.battery < CHARGED)) else home
        if at == want:
            self._dock()
            return
        self.leg, self.target = ("to_charge" if want == CHARGE_NODE else "to_home"), want
        self.task_text = f"Returning to {label(want)}"
        self.decision = "Battery low: self-dispatching to the charge bay" if want == CHARGE_NODE else "Task done: returning home"
        self._route_leg()

    def _dock(self) -> None:
        node = node_at(self.pos)
        self.leg, self.target, self.task = "idle", None, None
        self.path, self.path_index, self.progress = [self.pos.model_copy()], 0, 0.0
        self.waiting, self.stuck, self.wait_for = 0, 0, None
        self.claims.clear()  # docked robots sit in bays, off the traffic lanes
        charging = node == CHARGE_NODE and self.battery < CHARGED
        self.status = "Charging" if charging else "Idle"
        self.task_text = f"Charging at {label(node)}" if charging else f"Standby at {label(node)}"
        self.decision = "Charging at the bay" if charging else "Docked; listening for task announcements"

    def _free(self) -> None:
        self.task, self.payload, self.priority = None, 0.0, BASE_PRIORITY
        self.leg, self.target = "idle", None
        if self.status in ("Task handoff", "Blocked"):
            self.status = "Moving"
        self._send_idle()

    def _docked(self) -> bool:
        return self.leg == "idle" and self.progress == 0.0 and node_at(self.pos) is not None

    # ── mutex zones: leases are claims on the mesh ───────────────────────

    def zone_claims(self) -> set[str]:
        held = (self.claims | {z for z, until in self.manual_leases.items() if until > self.clock.tick}) & MUTEX_ZONES
        return held

    def request_zone(self, zone: str, ticks: int) -> bool:
        """Operator/peer-protocol request: lease a zone for `ticks` if no live peer holds it."""
        self.say("MUTEX_REQ", {"zone": zone, "pri": self.priority, "ttl": ticks})
        holder = self._claimant(zone)
        if holder and holder != self.id:
            self.say("YIELD_ACK", {"zone": zone, "to": holder}, to=holder)
            return False
        self.manual_leases[zone] = self.clock.tick + ticks
        self._sync_leases()
        return True

    def publish_intent(self, zone: str, eta: float) -> None:
        self.say("MUTEX_REQ", {"zone": zone, "pri": self.priority, "eta": eta, "intent": True})

    def _sync_leases(self) -> None:
        now = self.zone_claims()
        for zone in now - self.leased:
            self.say("MUTEX_GRANT", {"zone": zone, "pri": self.priority, "ttl": LEASE_TICKS})
            self.log("LEASE", f"{self.id} leased {zone} for {LEASE_TICKS * CONTROL_PERIOD_SECONDS:.1f}s | priority {self.priority}")
        for zone in self.leased - now:
            self.say("MUTEX_RELEASE", {"zone": zone})
            self.log("LEASE", f"{self.id} cleared {zone} & released distributed mutex")
        self.leased = now

    def _outranked(self, zone: str, hops: int) -> str | None:
        """A closer (or equal-distance, higher-utility) live peer has announced it is heading for this zone."""
        if self.sw or self.stuck >= self.deadlock_ticks:
            return None
        mine = self._rank()
        for peer in self._live_peers():
            if peer.intent_zone == zone and peer.intent_hops <= hops and peer.rank > mine:
                return peer.id
        return None

    # ── waiting / deadlock resolution ────────────────────────────────────

    def _wait(self, node: str, holder: str, reason: str) -> None:
        self.waiting += 1
        self.stuck += 1
        count = self.stuck
        self.wait_for, self.wait_node = holder, node
        here = node_at(self.pos) if self.progress == 0.0 else None
        if here is not None:  # a stationary robot only needs its own node; don't hold others' exits hostage
            self.claims &= {here}
        if self.status != "Yielding":
            self.status = "Yielding"
            self.say("YIELD_ACK", {"to": holder, "reason": reason}, to=holder)
            self.log("LEASE", f"{self.id} yielding at safety line | {reason}")
        self.decision = f"Yielding: {reason}"
        if not self.sw and count >= PATIENCE_TICKS and self.clock.tick - self.last_detour >= DETOUR_COOLDOWN_TICKS and self.target and self._patient_detour(node):
            return
        if count % self.deadlock_ticks != 0 or not self.target:
            return
        cycle = self._deadlock_cycle()
        if cycle:  # mutual wait: every member computes the same victim from shared data; only the victim moves
            movers = []
            for member in cycle:
                aside = self._side_step_path(member, cycle)
                if aside is not None:
                    movers.append((member, aside))
            if movers:
                victim, aside = min(movers, key=lambda m: self._member_rank(m[0]))
                if victim == self.id:
                    self._side_step(aside, self.wait_for or holder)
                return
        detour = self._plan(self.target, {node})
        if detour is not None:
            self._apply_path(detour)
            self.status = "Rerouting"
            self.waiting, self.wait_for = 0, None
            self.decision = f"Detouring around occupied {label(node)}"
            self.log("REROUTE", f"{self.id} detoured around occupied {label(node)}")

    def _patient_detour(self, node: str) -> bool:
        """Blocked for a few ticks: reroute around the blocker if the extra travel is cheaper than waiting it out."""
        detour = self._plan(self.target, {node}, soft=True)
        if detour is None:
            return False
        current = path_length(_xy(self.path[self.path_index:])) - self.progress * 0.0
        extra_ticks = (path_length(_xy(detour)) - current) / self.spec.speed
        if extra_ticks > DETOUR_BUDGET_TICKS:
            return False
        self._apply_path(detour)
        self.status = "Rerouting"
        self.waiting, self.wait_for = 0, None
        self.last_detour = self.clock.tick
        self.decision = f"Detouring around {label(node)}: {extra_ticks:.0f} extra ticks beats waiting"
        self.log("REROUTE", f"{self.id} detoured around busy {label(node)} (+{max(0, extra_ticks):.0f} ticks) instead of waiting")
        return True

    def _member_rank(self, rid: str) -> tuple[float, str]:
        return self._rank() if rid == self.id else self.peers[rid].rank

    def _wait_edge(self, rid: str) -> str | None:
        return self.wait_for if rid == self.id else (self.peers[rid].wait_for if rid in self.peers and self._alive(self.peers[rid]) else None)

    def _deadlock_cycle(self) -> list[str] | None:
        chain: list[str] = []
        current = self.id
        while current not in chain and (nxt := self._wait_edge(current)) is not None and (nxt == self.id or nxt in self.peers):
            chain.append(current)
            current = nxt
        return chain[chain.index(current):] if current in chain else None

    def _member_view(self, rid: str) -> tuple[str | None, tuple[float, float], list[str], str | None]:
        if rid == self.id:
            here = node_at(self.pos) if self.progress == 0.0 else None
            return here, (self.pos.x, self.pos.y), [n for n in (node_at(p) for p in self.path[self.path_index:]) if n], self.wait_node
        p = self.peers[rid]
        return p.node, (p.x, p.y), list(p.path_nodes), p.wait_node

    def _side_step_path(self, rid: str, cycle: list[str]) -> list[str] | None:
        """Shortest way for `rid` to park on a node that no other robot in the deadlock needs. It may pass through
        (unclaimed) nodes those robots will use later: this is what lets two robots swap dead-end racks."""
        here, xy, _, wait_node = self._member_view(rid)
        if here is None:
            return None
        avoid: set[str | None] = {wait_node}
        for other in cycle:
            if other != rid:
                avoid.update(self._member_view(other)[2])
        taken = set(self._ghosts()) | self.blocked
        for peer in self._live_peers():
            if peer.id != rid and not peer.docked:
                taken |= peer.claims
        if rid != self.id:
            taken |= self.claims
        queue: list[tuple[str, list[str]]] = [(here, [])]
        seen = {here}
        while queue:
            node, route = queue.pop(0)
            if len(route) >= 4:
                continue
            for n in sorted(GRAPH_ADJACENCY[node], key=lambda n: math.hypot(WAREHOUSE_NODES[n].x - xy[0], WAREHOUSE_NODES[n].y - xy[1])):
                if n in seen or n in taken:
                    continue
                seen.add(n)
                if n not in avoid:
                    return route + [n]
                queue.append((n, route + [n]))
        return None

    def _side_step(self, route: list[str], other: str) -> None:
        self._apply_path([self.pos.model_copy()] + points(route))
        self.status = "Rerouting"
        self.waiting, self.wait_for = 0, None
        self.hold_until = self.clock.tick + HOLD_TICKS + 2 * (len(route) - 1)
        self.decision = f"Stepped aside to {label(route[-1])}: lowest utility in a deadlock with {other}"
        self.log("REROUTE", f"{self.id} stepped aside to {label(route[-1])} to clear a deadlock with {other}")

    # ── movement ─────────────────────────────────────────────────────────

    def _hold_applies(self) -> bool:
        return self.target is not None and node_at(self.pos) != self.target and self.path_index == 0

    def _move(self) -> None:
        if self.status == "Blocked":
            return
        if self.path_index + 1 >= len(self.path):
            if self.leg != "idle":
                self._arrive()
            return
        start, end = self.path[self.path_index], self.path[self.path_index + 1]
        if self.progress == 0.0:
            if self.hold_until > self.clock.tick and self._hold_applies():
                return
            here = node_at(start)
            needed = [n for n in (node_at(p) for p in self.path[self.path_index + 1: self.path_index + 1 + self.lookahead]) if n]
            holder = self._claimant(here)
            if here and holder:
                return self._wait(here, holder, f"{label(here)} occupied by {holder}")
            if here:
                self.claims.add(here)  # an undocked robot always claims the node it stands on
            ghosts = self._ghosts()
            for hops, node in enumerate(needed, start=1):
                if node in ghosts:
                    return self._wait(node, "MESH", f"{label(node)} blocked by a silent robot")
                holder = self._claimant(node)
                if holder:
                    return self._wait(node, holder, f"{label(node)} reserved by {holder}")
                if node in MUTEX_ZONES:
                    rival = self._outranked(node, hops)
                    if rival:
                        return self._wait(node, rival, f"{node} yielded to {rival}: closer or higher utility (rank {self._rival_rank(rival):.1f} vs {self._rank()[0]:.1f})")
            self.claims.update(needed)
            if here:
                self.claims.discard(here)
        self.waiting, self.stuck, self.wait_for, self.wait_node = 0, 0, None, None

        distance = math.hypot(end.x - start.x, end.y - start.y) or 1.0
        progress = self.progress + self.spec.speed / distance
        self.battery = max(0.0, self.battery - DRAIN_PER_TICK)
        if self.status != "Rerouting":
            self.status = "Moving"
        if progress >= 1.0:
            self.path_index += 1
            self.progress = 0.0
            self.pos = end.model_copy()
            self._prune()
            if self.status == "Rerouting":
                self.status = "Moving"
        else:
            self.progress = progress
            self.pos = Point(x=start.x + (end.x - start.x) * progress, y=start.y + (end.y - start.y) * progress)

    def _rival_rank(self, rid: str) -> float:
        return self.peers[rid].rank[0]

    def _arrive(self) -> None:
        if self.target and node_at(self.pos) != self.target:
            self._route_leg()  # ended a side-step / partial path: continue to the real target
            return
        task = self.task
        if self.leg == "to_pickup" and task:
            self.payload = task["payload_kg"]
            self.leg, self.target = "to_drop", task["destination"]
            self.say("TASK_STATUS", {"task_id": task["id"], "status": "In Progress", "robot": self.id})
            self.log("HANDOFF", f"{self.id} loaded {task['payload_kg']:g}kg at {label(task['pickup'])} for {task['id']}")
            self.decision = f"Loaded {task['id']}; delivering to {label(task['destination'])}"
            self._route_leg()
        elif self.leg == "to_drop" and task:
            self.completed += 1
            self.say("TASK_STATUS", {"task_id": task["id"], "status": "Completed", "robot": self.id})
            self.log("HANDOFF", f"{task['id']} delivered to {label(task['destination'])} by {self.id}")
            self.owners.pop(task["id"], None)
            self.task, self.payload, self.priority = None, 0.0, BASE_PRIORITY
            self.leg, self.target = "idle", None
            self._send_idle()
        else:
            self._dock()

    # ── fault injection: radio blackout ──────────────────────────────────

    def go_silent(self, ticks: int = DROPOUT_TICKS) -> None:
        self.log("HEARTBEAT", f"Agent dropout simulated on {self.id} · safety stop active, radio silent")
        self.say("AGENT_FAULT", {"fault": self.id, "note": "radio dropout / heartbeat silent"})
        self.online = False
        self.bus.silenced.add(self.id)
        self.offline_until = self.clock.tick + ticks
        self.status, self.task_text = "Blocked", "Communications offline (failsafe)"
        self.decision = "Radio silent: safety stop until the link returns"
        self.publish_pose()

    def _rejoin(self) -> None:
        self.online = True
        self.bus.silenced.discard(self.id)
        self.bus.inboxes[self.id].clear()
        released = self.task
        self.task, self.payload, self.priority = None, 0.0, BASE_PRIORITY
        self.leg, self.target, self.waiting, self.wait_for = "idle", None, 0, None
        self.peers = {}
        self.owners.clear()
        self.auctions.clear()
        self.claims &= {node_at(self.pos)}
        self.status = "Moving"
        self.log("HEARTBEAT", f"{self.id} radio restored · rejoining the mesh")
        self.say("SYNC_REQ", {})
        if released:
            self.say("TASK_STATUS", {"task_id": released["id"], "status": "Released", "robot": self.id})
        self.decision = "Rejoined the mesh; requested peer state"
        self._send_idle()

    def set_battery(self, battery: float) -> None:
        self.battery = max(0.0, min(100.0, battery))
        self.say("HEARTBEAT", {"alert": f"BATTERY_ALERT[{self.battery:.0f}% low power trigger]"})
        self.log("HEARTBEAT", f"{self.id} battery reduced to {self.battery:.0f}% · opportunity charge safeguard triggered")
        if self.battery < LOW_BATTERY and self.is_free():
            self._send_idle()
        elif self.battery < LOW_BATTERY and self.task and self.leg == "to_pickup":
            # Not carrying anything yet: hand the job back to the mesh so a healthier peer takes it, then go charge.
            task = self.task
            self.say("TASK_STATUS", {"task_id": task["id"], "status": "Released", "robot": self.id})
            self.owners.pop(task["id"], None)
            self.log("HANDOFF", f"{self.id} is too low to work: handing {task['id']} back to the mesh and heading to charge")
            self._free()
            self.decision = f"Battery {self.battery:.0f}%: handed {task['id']} back to peers, going to charge"

    # ── one control cycle ────────────────────────────────────────────────

    def step(self) -> None:
        if not self.online:
            if self.clock.tick < self.offline_until:
                return
            self._rejoin()
        self.handle_inbox()
        self._expire_peers()
        self._close_stale_auctions()
        for zone in [z for z, until in self.manual_leases.items() if until <= self.clock.tick]:
            del self.manual_leases[zone]
        if self.status == "Blocked" and self.target and self.clock.tick % 3 == 0:
            self._route_leg(announce=False)
        if self.leg == "idle" and self.status != "Blocked" and self._docked():
            self._send_idle()
        self._move()
        if self._docked() and node_at(self.pos) == CHARGE_NODE and self.battery < 100.0:
            self.battery = min(100.0, self.battery + CHARGE_RATE)
            self.status = "Charging" if self.battery < CHARGED else "Idle"
        self._sync_leases()
        self.publish_pose()

    async def run(self, running: Callable[[], bool], phase: float) -> None:
        """Live mode: this robot's own control loop, independent of every other robot's."""
        await asyncio.sleep(phase)
        while True:
            await asyncio.sleep(CONTROL_PERIOD_SECONDS)
            if running():
                self.step()

    async def listen(self) -> None:
        """Live mode: react to mesh messages the moment they arrive (bids, awards, obstacle alerts)."""
        waker = self.bus.waker(self.id)
        while True:
            await waker.wait()
            waker.clear()
            self.handle_inbox()

    # ── telemetry ────────────────────────────────────────────────────────

    def publish_pose(self) -> None:
        here = node_at(self.pos) if self.progress == 0.0 else None
        occupied = [here] if here else [n for n in (node_at(self.path[self.path_index]), node_at(self.path[min(self.path_index + 1, len(self.path) - 1)])) if n]
        upcoming = [n for n in (node_at(p) for p in self.path[self.path_index + 1:]) if n]
        zone = next(((z, i + 1) for i, z in enumerate(upcoming[:INTENT_HORIZON]) if z in MUTEX_ZONES and z not in self.claims), None)
        moving = self.status in ("Moving", "Yielding", "Rerouting", "Task handoff")
        docked = self._docked()
        self.world.pos[self.id] = (self.pos.x, self.pos.y)
        self.world.docked[self.id] = docked
        self.world.parked[self.id] = here is not None and here in self.world.staging and here not in self.claims
        self.say("POSE", {
            "x": self.pos.x, "y": self.pos.y, "node": here,
            "claims": [] if docked else sorted(self.claims | (self.zone_claims())),
            "occupied": occupied, "status": self.status, "battery": self.battery, "priority": self.priority,
            "wait_for": self.wait_for if self.status == "Yielding" else None, "wait_node": self.wait_node,
            "path_nodes": upcoming, "intent_zone": zone[0] if zone and moving else None, "intent_hops": zone[1] if zone else 99,
            "docked": docked,
        })

    def telemetry(self) -> RobotState:
        return RobotState(
            id=self.id, name=self.spec.name, color=self.spec.color, battery=self.battery, status=self.status,
            task=self.task_text, task_id=self.task["id"] if self.task else None, leg=self.leg, target=self.target,
            priority=self.priority, path=[p.model_copy() for p in self.path], path_index=self.path_index,
            progress=self.progress, position=self.pos.model_copy(), completed=self.completed,
            payload_capacity_kg=self.spec.max_payload, current_payload_kg=self.payload,
            decision=self.decision, online=self.online,
        )

    def closest_node(self) -> str:
        return node_at(self.pos) or find_closest_node(self.pos.x, self.pos.y)
