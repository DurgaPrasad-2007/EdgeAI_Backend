from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress
from datetime import datetime, timezone

from app.audit import SYSTEM_ACTOR, actor_var
from app.cache import CacheManager
from app.database import Database
from app.graph import (
    CHARGE_NODE,
    GRAPH_ADJACENCY,
    MUTEX_ZONES,
    ROBOT_FLEET,
    WAREHOUSE_NODES,
    RobotSpec,
    find_closest_node,
    find_shortest_node_path,
    path_length,
    resolve_node,
    world_payload,
)
from app.schemas import (
    AuditRecord,
    BlockageRequest,
    FleetEvent,
    FleetState,
    IntentRequest,
    Kpis,
    P2PMessage,
    Point,
    ReservationRequest,
    RobotState,
    SimulationControl,
    TaskBidRequest,
    TaskCreate,
    TaskRecord,
    TaskUpdate,
)
from app.task_store import TaskStore

log = logging.getLogger("edgefleet.coordinator")

CONTROL_PERIOD_SECONDS = 0.6
LOW_BATTERY = 30.0          # idle robots below this go to the charge bay
MIN_BID_BATTERY = 20.0      # robots below this may not bid
CHARGED = 95.0
CHARGE_RATE = 1.5           # % per tick while docked at the charge bay
DRAIN_PER_TICK = 0.08       # % per tick while moving
LEASE_TICKS = 8
HEARTBEAT_EVERY = 8
DEADLOCK_TICKS = 12         # ticks a robot waits before it tries a detour / side-step
HOLD_TICKS = 15             # a robot that stepped aside stays put this long so the other can pass
LOOKAHEAD_NODES = 2         # nodes a robot must reserve (all-or-nothing) before it moves
COLLISION_RADIUS = 10.0
BASE_PRIORITY = 50
MAX_EVENTS = 30
MAX_P2P = 12
RECENT_COMPLETED = 50
URGENCY_BONUS = {"low": 0, "standard": 5, "critical": 25}

_COORD_NODE = {(round(n.x), round(n.y)): n.id for n in WAREHOUSE_NODES.values()}


class FleetError(ValueError):
    """Invalid request (unknown robot / location / task)."""


class FleetConflict(FleetError):
    """Request is valid but conflicts with current fleet state."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _node_at(point: Point) -> str | None:
    return _COORD_NODE.get((round(point.x), round(point.y)))


def _points(node_ids: list[str]) -> list[Point]:
    return [Point(x=WAREHOUSE_NODES[i].x, y=WAREHOUSE_NODES[i].y) for i in node_ids]


def _xy(points: list[Point]) -> list[tuple[float, float]]:
    return [(p.x, p.y) for p in points]


def _label(node_id: str) -> str:
    return WAREHOUSE_NODES[node_id].label


def initial_state(fleet: tuple[RobotSpec, ...] = ROBOT_FLEET, running: bool = True) -> FleetState:
    robots = []
    for spec in fleet:
        home = WAREHOUSE_NODES[spec.home]
        robots.append(
            RobotState(
                id=spec.id,
                name=spec.name,
                color=spec.color,
                battery=spec.battery,
                status="Idle",
                task=f"Standby at {home.label}",
                priority=BASE_PRIORITY,
                path=[Point(x=home.x, y=home.y)],
                path_index=0,
                progress=0.0,
                position=Point(x=home.x, y=home.y),
                completed=0,
                payload_capacity_kg=spec.max_payload,
            )
        )
    return FleetState(
        tick=0,
        running=running,
        aisle_blocked=False,
        reservation=None,
        lease_until=0,
        completed_tasks=0,
        collision_count=0,
        messages=0,
        events=[FleetEvent(id=1, time="T+00.0s", type="HEARTBEAT", message=f"Fleet coordinator online | {len(robots)} AMRs registered")],
        robots=robots,
    )


class FleetCoordinator:
    """Authoritative fleet state: world graph, robots, task lifecycle, leases and KPIs.

    Every derived value the UI shows (paths, statuses, events, P2P packets, KPIs, tasks) comes from here.
    """

    def __init__(self, database: Database, cache: CacheManager | None = None, fleet: tuple[RobotSpec, ...] = ROBOT_FLEET) -> None:
        self._fleet = {spec.id: spec for spec in fleet}
        self._fleet_specs = fleet
        self._cache = cache
        self._task_store = TaskStore(database.sessions, database.url, cache=cache)
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[FleetState]] = set()
        self._ticker: asyncio.Task[None] | None = None
        self._tasks: dict[str, TaskRecord] = {}
        self._pending: list[tuple[str, dict]] = []
        self._pending_audit: list[dict] = []
        self._reset_runtime(running=True)

    # ── lifecycle ────────────────────────────────────────────────────────

    def _reset_runtime(self, running: bool) -> None:
        self._state = initial_state(self._fleet_specs, running)
        self._owner: dict[str, str] = {}         # node -> robot id (in-motion reservations)
        self._lease_until: dict[str, int] = {}   # mutex zone -> tick
        self._waiting: dict[str, int] = {}
        self._wait_for: dict[str, str] = {}      # robot -> robot it is waiting on
        self._wait_node: dict[str, str] = {}     # robot -> node it is waiting for
        self._hold: dict[str, int] = {}          # robot -> tick until which it stays aside
        self._collided: set[frozenset[str]] = set()
        self._event_seq = 1
        self._p2p_seq = 0
        self._session_completed = 0
        self._state.completed_tasks = sum(1 for t in self._tasks.values() if t.status == "Completed")

    async def start(self, ticker: bool = True) -> None:
        await self._task_store.requeue_orphans()
        for task in await self._task_store.list():
            task.created_at = _aware(task.created_at)
            self._tasks[task.id] = task
            # Rows written before locations were validated: canonicalise what we can ("Dock E" -> DOCK-E).
            pickup, destination = resolve_node(task.pickup), resolve_node(task.destination)
            if pickup and destination and (pickup, destination) != (task.pickup, task.destination):
                self._set_task(task, pickup=pickup, destination=destination)
        self._state.completed_tasks = sum(1 for t in self._tasks.values() if t.status == "Completed")
        async with self._lock:
            self._auction()
        await self._flush()
        if ticker:
            self._ticker = asyncio.create_task(self._tick_loop(), name="edgefleet-simulation-ticker")

    async def stop(self) -> None:
        if self._ticker is None:
            return
        self._ticker.cancel()
        with suppress(asyncio.CancelledError):
            await self._ticker
        self._ticker = None

    def world(self) -> dict:
        payload = world_payload(self._fleet_specs)
        payload["config"] = {
            "control_period_s": CONTROL_PERIOD_SECONDS,
            "low_battery_pct": LOW_BATTERY,
            "min_bid_battery_pct": MIN_BID_BATTERY,
            "collision_radius": COLLISION_RADIUS,
        }
        return payload

    async def snapshot(self) -> FleetState:
        async with self._lock:
            return self._snapshot(bump=False)

    async def reset(self) -> FleetState:
        async with self._lock:
            running = self._state.running
            # Robots return to their docks, so in-flight tasks go back to the queue.
            for task in self._tasks.values():
                if task.status in ("Assigned", "In Progress"):
                    self._set_task(task, status="Queued", unassign=True)
            self._reset_runtime(running)
            self._auction()
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    async def set_simulation(self, control: SimulationControl) -> FleetState:
        async with self._lock:
            self._state.running = control.running
            self._event("HEARTBEAT", f"Simulation {'resumed' if control.running else 'paused'}")
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    # ── peer protocol API ────────────────────────────────────────────────

    async def report_intent(self, intent: IntentRequest) -> FleetState:
        async with self._lock:
            self._robot(intent.robot_id)
            self._p2p(intent.robot_id, "MESH", "MUTEX_REQ", f"INTENT[{intent.corridor_id}, ETA={intent.eta_seconds:.1f}s]")
            self._event("INTENT", f"{intent.robot_id} published {intent.corridor_id} intent (ETA {intent.eta_seconds:.1f} s)")
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    async def request_reservation(self, request: ReservationRequest) -> FleetState:
        async with self._lock:
            robot = self._robot(request.robot_id)
            zone = resolve_node(request.corridor_id)
            ticks = max(1, round(request.lease_seconds / CONTROL_PERIOD_SECONDS))
            if zone not in MUTEX_ZONES:
                self._event("LEASE", f"{robot.id} local lease for {request.corridor_id} acknowledged (not a mutex zone)")
            else:
                self._p2p(robot.id, "MESH", "MUTEX_REQ", f"MUTEX_REQ[{zone}, lease={request.lease_seconds:.1f}s, pri={robot.priority}]")
                holder_id = self._state.leases.get(zone)
                if holder_id in (None, robot.id):
                    self._grant(zone, robot, ticks, f"{robot.id} leased {zone} for {request.lease_seconds:.1f} s | priority {robot.priority}")
                else:
                    holder = self._robot(holder_id)
                    inside = self._owner.get(zone) == holder_id  # holder is in / about to enter the zone
                    if not inside and max((holder, robot), key=self._rank).id == robot.id:
                        self._grant(zone, robot, ticks, f"{robot.id} preempted {zone} lease after deterministic peer scoring")
                    else:
                        self._p2p(robot.id, holder_id, "YIELD_ACK", f"YIELD[{zone} held by {holder_id}]")
                        self._event("LEASE", f"{robot.id} yielded {zone} to {holder_id}; safety buffer maintained")
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    async def report_blockage(self, request: BlockageRequest) -> FleetState:
        async with self._lock:
            node = resolve_node(request.aisle_id)
            if node is None:
                raise FleetError(f"Unknown aisle/node '{request.aisle_id}'")
            blocked = set(self._state.blocked_nodes)
            if request.blocked and node not in blocked:
                blocked.add(node)
                self._state.blocked_nodes = sorted(blocked)
                reporter = min(self._state.robots, key=lambda r: math.hypot(r.position.x - WAREHOUSE_NODES[node].x, r.position.y - WAREHOUSE_NODES[node].y))
                self._p2p(reporter.id, "MESH", "OBSTACLE_ALERT", f"OBSTACLE_DETECTED[{_label(node)} impassable]")
                self._event("REROUTE", f"Obstacle reported at {_label(node)} · replanning affected routes")
                for robot in self._state.robots:
                    ahead = [_node_at(p) for p in robot.path[robot.path_index + 1:]]
                    if node in ahead and robot.leg != "idle":
                        self._replan(robot, force=True)
            elif not request.blocked and node in blocked:
                blocked.discard(node)
                self._state.blocked_nodes = sorted(blocked)
                self._event("REROUTE", f"Obstacle cleared at {_label(node)} · restoring optimal routes")
                for robot in self._state.robots:
                    if robot.leg != "idle":
                        self._replan(robot, force=False)
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    async def set_robot_battery(self, robot_id: str, battery: float) -> FleetState:
        async with self._lock:
            robot = self._robot(robot_id)
            robot.battery = max(0.0, min(100.0, float(battery)))
            self._p2p(robot.id, "MESH", "HEARTBEAT", f"BATTERY_ALERT[{robot.battery:.0f}% low power trigger]")
            self._event("HEARTBEAT", f"{robot.id} battery reduced to {robot.battery:.0f}% · opportunity charge safeguard triggered")
            if robot.battery < LOW_BATTERY and self._is_free(robot):
                self._send_idle(robot)
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    async def simulate_agent_failure(self, robot_id: str) -> FleetState:
        async with self._lock:
            robot = self._robot(robot_id)
            for zone, holder in list(self._state.leases.items()):
                if holder == robot.id:
                    self._release_lease(zone, robot.id, "simulated heartbeat loss on")
            self._p2p(robot.id, "MESH", "OBSTACLE_ALERT", f"AGENT_FAULT[{robot.id} radio dropout / heartbeat silent]")
            self._event("HEARTBEAT", f"Agent dropout simulated on {robot.id} · leases revoked & safety stop active")
            robot.status = "Blocked"
            robot.task = "Communications offline (failsafe)"
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    async def bid_for_task(self, request: TaskBidRequest) -> FleetState:
        async with self._lock:
            task = self._tasks.get(request.task_id)
            if task is None:
                raise FleetError(f"Unknown task {request.task_id}")
            if resolve_node(request.destination) != task.destination:
                raise FleetConflict(f"Destination does not match task {task.id}")
            if task.status not in ("Queued", "Blocked"):
                raise FleetConflict(f"Task {task.id} is {task.status}; only queued tasks can be auctioned")
            pool = [self._robot(rid) for rid in request.candidate_robot_ids]
            pool = [r for r in pool if self._is_free(r)]
            if self._auction_one(task, pool) is None:
                raise FleetConflict(f"No eligible candidate for {task.id}")
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return snapshot

    # ── task API ─────────────────────────────────────────────────────────

    async def create_task(self, request: TaskCreate) -> TaskRecord:
        pickup, destination = resolve_node(request.pickup), resolve_node(request.destination)
        if pickup is None or destination is None:
            raise FleetError(f"Unknown location: {request.pickup if pickup is None else request.destination}")
        if pickup == destination:
            raise FleetError("Pickup and destination must differ")
        async with self._lock:
            task = await self._task_store.create(
                pickup=pickup, destination=destination, priority=request.priority, assigned_robot_id=None,
                payload_kg=request.payload_kg, payload_size=request.payload_size, urgency=request.urgency,
            )
            task.created_at = _aware(task.created_at)
            self._tasks[task.id] = task
            self._event("HANDOFF", f"{task.id} queued: {_label(pickup)} → {_label(destination)} ({task.payload_kg:g}kg {task.payload_size})")
            self._auction()
            result = task.model_copy()
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return result

    async def update_task(self, task_id: str, request: TaskUpdate) -> TaskRecord | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            self._apply_update(task, request)
            self._auction()
            result = task.model_copy()
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return result

    async def complete_task(self, task_id: str) -> TaskRecord | None:
        return await self.update_task(task_id, TaskUpdate(status="Completed"))

    async def delete_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is None:
                return False
            self._release_robot_of(task)
            await self._task_store.delete(task_id)
            self._event("HANDOFF", f"Task {task_id} withdrawn from auction queue")
            self._auction()
            snapshot = self._snapshot()
        await self._publish(snapshot)
        return True

    async def list_tasks(self) -> list[TaskRecord]:
        async with self._lock:
            return [t.model_copy() for t in sorted(self._tasks.values(), key=lambda t: _aware(t.created_at), reverse=True)]

    def _apply_update(self, task: TaskRecord, request: TaskUpdate) -> None:
        if task.status == "Completed" and (request.status not in (None, "Completed") or request.assigned_robot_id):
            raise FleetConflict(f"Task {task.id} is already completed")
        robot = self._robot_of(task)
        if request.payload_kg is not None and robot and request.payload_kg > self._fleet[robot.id].max_payload:
            raise FleetConflict(f"{robot.id} cannot carry {request.payload_kg:g}kg")
        changes = {k: v for k, v in request.model_dump(exclude={"status", "assigned_robot_id"}).items() if v is not None}
        if changes:
            self._set_task(task, **changes)
            if "priority" in changes and robot:
                robot.priority = task.priority
            self._event("HANDOFF", f"Task {task.id} attributes reconfigured ({', '.join(f'{k}={v}' for k, v in changes.items())})")

        if request.assigned_robot_id and request.assigned_robot_id != task.assigned_robot_id:
            target = self._robot(request.assigned_robot_id)
            if not self._is_free(target) or self._bid(target, task) is None:
                raise FleetConflict(f"{target.id} cannot take {task.id} right now")
            if robot:
                self._release_robot_of(task)
            self._assign(task, target)
            self._event("HANDOFF", f"{task.id} manually reassigned to {target.id}")
        if request.status and request.status != task.status:
            if request.status == "Completed":
                self._release_robot_of(task)
                self._complete(task)
            elif request.status in ("Queued", "Blocked"):
                self._release_robot_of(task)
                self._set_task(task, status=request.status, unassign=True)
            else:
                raise FleetConflict(f"Status {request.status} is set by the fleet, not by clients")

    # ── knowledge ────────────────────────────────────────────────────────

    async def add_knowledge(self, item):
        return await self._task_store.add_knowledge(item)

    async def search_knowledge(self, embedding: list[float], limit: int):
        return await self._task_store.search_knowledge(embedding, limit)

    async def seed_knowledge(self, items) -> int:
        if await self._task_store.knowledge_count():
            return 0
        for item in items:
            await self._task_store.add_knowledge(item)
        return len(items)

    # ── pub/sub ──────────────────────────────────────────────────────────

    async def subscribe(self) -> asyncio.Queue[FleetState]:
        queue: asyncio.Queue[FleetState] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[FleetState]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(CONTROL_PERIOD_SECONDS)
            try:
                async with self._lock:
                    if not self._state.running:
                        continue
                    self._step()
                    snapshot = self._snapshot()
                await self._publish(snapshot)
            except Exception:  # keep the fleet alive; the next tick retries
                log.exception("simulation tick failed")

    async def _publish(self, snapshot: FleetState) -> None:
        await self._flush()
        if self._cache:
            with suppress(Exception):
                await self._cache.set_json("edgefleet:state:latest", snapshot.model_dump(mode="json"), ttl=10)
        for queue in tuple(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(snapshot)

    async def audit(self, category: str, message: str, actor: str | None = None) -> None:
        """Record an event that is not part of the simulation (logins, admin changes)."""
        self._pending_audit.append({"actor": actor or actor_var.get(), "category": category, "message": message})
        await self._flush()

    async def list_audit(self, limit: int = 200, category: str | None = None) -> list[AuditRecord]:
        await self._flush()
        return await self._task_store.list_audit(limit, category)

    async def _flush(self) -> None:
        """Persist task transitions and audit rows in order, outside the state lock."""
        async with self._flush_lock:
            rows, self._pending_audit = self._pending_audit, []
            if rows:
                try:
                    await self._task_store.add_audit(rows)
                except Exception:
                    log.exception("failed to persist %d audit rows", len(rows))
            while self._pending:
                task_id, fields = self._pending.pop(0)
                try:
                    await self._task_store.update(task_id, **fields)
                except Exception:
                    log.exception("failed to persist task %s", task_id)

    # ── snapshot / KPIs ──────────────────────────────────────────────────

    def _snapshot(self, bump: bool = True) -> FleetState:
        s = self._state
        if bump:
            s.seq += 1
        zone = min(MUTEX_ZONES) if MUTEX_ZONES else None
        s.reservation = s.leases.get(zone) if zone else None
        s.lease_until = self._lease_until.get(zone, 0) if s.reservation else 0
        s.aisle_blocked = bool(s.blocked_nodes)
        ordered = sorted(self._tasks.values(), key=lambda t: _aware(t.created_at), reverse=True)
        done = [t for t in ordered if t.status == "Completed"][:RECENT_COMPLETED]
        s.tasks = [t for t in ordered if t.status != "Completed"] + done
        s.kpis = self._kpis()
        return s.model_copy(deep=True)

    def _kpis(self) -> Kpis:
        s = self._state
        n = max(1, len(s.robots))
        hours = s.tick * CONTROL_PERIOD_SECONDS / 3600
        return Kpis(
            fleet_utilization_pct=round(100 * sum(1 for r in s.robots if r.task_id) / n),
            avg_battery_pct=round(sum(r.battery for r in s.robots) / n),
            collision_count=s.collision_count,
            active_leases=len(s.leases),
            operational_pct=round(100 * sum(1 for r in s.robots if r.status != "Blocked" and r.battery > 10) / n),
            completed_total=s.completed_tasks,
            queued_tasks=sum(1 for t in self._tasks.values() if t.status in ("Queued", "Blocked")),
            tasks_per_hour=round(self._session_completed / hours, 1) if hours > 0 else 0.0,
            tick=s.tick,
        )

    # ── event / message helpers ──────────────────────────────────────────

    def _stamp(self) -> str:
        return f"T+{self._state.tick * CONTROL_PERIOD_SECONDS:04.1f}s"

    def _event(self, type_: str, message: str) -> None:
        self._event_seq += 1
        stamp, actor = self._stamp(), actor_var.get()
        self._state.events.insert(0, FleetEvent(id=self._event_seq, time=stamp, type=type_, message=message))
        del self._state.events[MAX_EVENTS:]
        category = "TASK" if type_ == "HANDOFF" else "SYSTEM" if actor == SYSTEM_ACTOR else "CONTROL"
        self._pending_audit.append({"actor": actor, "category": category, "event_type": type_, "sim_time": stamp, "message": message})

    def _p2p(self, sender: str, recipient: str, type_: str, payload: str) -> None:
        self._p2p_seq += 1
        self._state.messages += 1
        self._state.p2p.insert(0, P2PMessage(id=f"p2p-{self._p2p_seq}", sender=sender, recipient=recipient, type=type_, payload=payload, timestamp=self._stamp()))
        del self._state.p2p[MAX_P2P:]

    def _set_task(self, task: TaskRecord, **fields) -> None:
        """Update the in-memory task and queue the same change for persistence."""
        for key, value in fields.items():
            if key == "unassign":
                task.assigned_robot_id = None
            else:
                setattr(task, key, value)
        self._pending.append((task.id, fields))

    # ── robots ───────────────────────────────────────────────────────────

    def _robot(self, robot_id: str) -> RobotState:
        for robot in self._state.robots:
            if robot.id == robot_id:
                return robot
        raise FleetError(f"Unknown robot {robot_id}")

    def _robot_of(self, task: TaskRecord) -> RobotState | None:
        return next((r for r in self._state.robots if r.task_id == task.id), None)

    @staticmethod
    def _rank(robot: RobotState) -> tuple[float, str]:
        return (robot.priority + (100.0 - robot.battery) * 0.2, robot.id)

    @staticmethod
    def _is_free(robot: RobotState) -> bool:
        return robot.task_id is None and robot.battery >= MIN_BID_BATTERY and robot.status != "Blocked"

    @staticmethod
    def _docked(robot: RobotState) -> bool:
        return robot.leg == "idle" and robot.progress == 0.0 and _node_at(robot.position) is not None

    # ── task allocation (Contract-Net) ───────────────────────────────────

    def _bid(self, robot: RobotState, task: TaskRecord) -> float | None:
        spec = self._fleet[robot.id]
        if task.payload_kg > spec.max_payload:
            return None
        route = self._plan(robot, task.pickup)
        if route is None:
            return None
        distance = path_length(_xy(route))
        return (spec.max_payload - task.payload_kg) * 0.05 + robot.battery * 0.40 - distance * 0.08 + task.priority * 0.25

    def _auction_one(self, task: TaskRecord, pool: list[RobotState]) -> RobotState | None:
        bids = [(score, robot) for robot in pool if (score := self._bid(robot, task)) is not None]
        if not bids:
            return None
        score, winner = max(bids, key=lambda b: (b[0], b[1].id))
        for other_score, robot in bids:
            if robot is not winner:
                self._p2p(robot.id, "MESH", "TASK_BID", f"BID[{task.id}, score={other_score:.1f}]")
        self._p2p(winner.id, "MESH", "TASK_BID", f"AUCTION_WIN[{task.id}, payload={task.payload_kg:g}kg ({task.payload_size}), pri={task.priority}, score={score:.1f}]")
        self._event("HANDOFF", f"{winner.id} won {task.id} ({task.payload_kg:g}kg {task.payload_size}) via Contract-Net auction (score {score:.1f})")
        self._assign(task, winner)
        return winner

    def _auction(self) -> None:
        pending = sorted(
            (t for t in self._tasks.values() if t.status in ("Queued", "Blocked")),
            key=lambda t: (-(t.priority + URGENCY_BONUS[t.urgency]), _aware(t.created_at)),
        )
        for task in pending:
            reason = None
            if task.pickup not in WAREHOUSE_NODES or task.destination not in WAREHOUSE_NODES:
                reason = "pickup or destination is not a location in the warehouse graph"
            elif not any(task.payload_kg <= spec.max_payload for spec in self._fleet.values()):
                reason = f"no AMR can carry {task.payload_kg:g}kg"
            elif find_shortest_node_path(task.pickup, task.destination, set(self._state.blocked_nodes)) is None:
                reason = "no route between pickup and destination"
            if reason:
                if task.status != "Blocked":
                    self._set_task(task, status="Blocked")
                    self._event("HANDOFF", f"{task.id} blocked: {reason}")
                continue
            if task.status == "Blocked":
                self._set_task(task, status="Queued")
            self._auction_one(task, [r for r in self._state.robots if self._is_free(r)])

    def _task_label(self, task: TaskRecord) -> str:
        return f"{task.id}: {task.pickup} → {task.destination} ({task.payload_kg:g}kg)"

    def _assign(self, task: TaskRecord, robot: RobotState) -> None:
        robot.task_id, robot.leg, robot.target = task.id, "to_pickup", task.pickup
        robot.priority, robot.current_payload_kg = task.priority, 0.0
        robot.task, robot.status = self._task_label(task), "Task handoff"
        self._waiting[robot.id] = 0
        self._wait_for.pop(robot.id, None)
        self._set_task(task, status="Assigned", assigned_robot_id=robot.id)
        self._route_leg(robot)

    def _complete(self, task: TaskRecord) -> None:
        if task.status != "Completed":
            self._set_task(task, status="Completed")
            self._state.completed_tasks += 1
            self._session_completed += 1

    def _release_robot_of(self, task: TaskRecord) -> None:
        robot = self._robot_of(task)
        if robot:
            self._free(robot)

    def _free(self, robot: RobotState) -> None:
        robot.task_id, robot.current_payload_kg, robot.priority = None, 0.0, BASE_PRIORITY
        robot.leg, robot.target = "idle", None
        if robot.status in ("Task handoff", "Blocked"):
            robot.status = "Moving"
        self._send_idle(robot)

    # ── routing ──────────────────────────────────────────────────────────

    @property
    def _blocked(self) -> set[str]:
        return set(self._state.blocked_nodes)

    def _plan(self, robot: RobotState, target: str, avoid: frozenset[str] | set[str] = frozenset()) -> list[Point] | None:
        """Route from the robot's physical position to `target`, never entering blocked nodes."""
        blocked = self._blocked | set(avoid)
        here = _node_at(robot.position) if robot.progress == 0.0 else None
        if here is not None:
            ids = find_shortest_node_path(here, target, blocked)
            return None if ids is None else _points(ids)
        # Mid-edge: continue to the node ahead or turn back to the node just left, whichever is cheaper.
        best: tuple[float, list[Point]] | None = None
        for idx in dict.fromkeys((robot.path_index, robot.path_index + 1)):
            if not 0 <= idx < len(robot.path):
                continue
            node = _node_at(robot.path[idx])
            if node is None or node in blocked:
                continue
            ids = find_shortest_node_path(node, target, blocked)
            if ids is None:
                continue
            route = _points(ids)
            cost = math.hypot(robot.path[idx].x - robot.position.x, robot.path[idx].y - robot.position.y) + path_length(_xy(route))
            if best is None or cost < best[0]:
                best = (cost, route)
        return None if best is None else [robot.position.model_copy()] + best[1]

    def _apply_path(self, robot: RobotState, route: list[Point]) -> None:
        # A robot turning around mid-edge keeps the node it was heading to until it reaches a node,
        # so nobody can enter the edge from that end head-on.
        mid_edge = robot.progress > 0.0 or _node_at(robot.position) is None
        ahead = robot.path_index + 1
        retained = _node_at(robot.path[ahead]) if mid_edge and ahead < len(robot.path) else None
        robot.path, robot.path_index, robot.progress = route, 0, 0.0
        self._prune_reservations(robot, extra={retained})

    def _prune_reservations(self, robot: RobotState, extra: set[str | None] | frozenset = frozenset()) -> None:
        """A robot owns its node plus the (up to) LOOKAHEAD_NODES path nodes it has reserved ahead."""
        window = {_node_at(robot.position), *extra}
        window.update(_node_at(p) for p in robot.path[robot.path_index + 1: robot.path_index + 1 + LOOKAHEAD_NODES])
        for node, holder in list(self._owner.items()):
            if holder == robot.id and node not in window:
                del self._owner[node]

    def _route_leg(self, robot: RobotState, announce: bool = True) -> bool:
        route = self._plan(robot, robot.target) if robot.target else None
        if route is None:
            if robot.status != "Blocked" and announce:
                self._event("REROUTE", f"{robot.id} has no route to {robot.target}; holding until the path clears")
            robot.status = "Blocked"
            return False
        self._apply_path(robot, route)
        if robot.status == "Blocked":
            robot.status = "Moving"
        return True

    def _replan(self, robot: RobotState, force: bool) -> None:
        if not robot.target:
            return
        route = self._plan(robot, robot.target)
        if route is None:
            self._route_leg(robot)
            return
        old = [_node_at(p) for p in robot.path[robot.path_index + 1:]]
        new = [n for n in (_node_at(p) for p in route) if n]
        if robot.progress == 0.0 and new and new[0] == _node_at(robot.position):
            new = new[1:]
        if force or new != old:
            self._apply_path(robot, route)
            robot.status = "Rerouting"
            self._event("REROUTE", f"{robot.id} replanned A* route to {_label(robot.target)}")

    def _send_idle(self, robot: RobotState) -> None:
        """Idle policy: charge when low, otherwise wait at the home dock."""
        at = _node_at(robot.position) if robot.progress == 0.0 else None
        home = self._fleet[robot.id].home
        want = CHARGE_NODE if (robot.battery < LOW_BATTERY or (at == CHARGE_NODE and robot.battery < CHARGED)) else home
        if at == want:
            self._dock(robot)
            return
        robot.leg, robot.target = ("to_charge" if want == CHARGE_NODE else "to_home"), want
        robot.task = f"Returning to {_label(want)}"
        self._route_leg(robot)

    def _dock(self, robot: RobotState) -> None:
        node = _node_at(robot.position)
        robot.leg, robot.target, robot.task_id = "idle", None, None
        robot.path, robot.path_index, robot.progress = [robot.position.model_copy()], 0, 0.0
        self._waiting[robot.id] = 0
        self._wait_for.pop(robot.id, None)
        for owned, holder in list(self._owner.items()):  # docked robots sit in bays, off the traffic lanes
            if holder == robot.id:
                del self._owner[owned]
        charging = node == CHARGE_NODE and robot.battery < CHARGED
        robot.status = "Charging" if charging else "Idle"
        robot.task = f"Charging at {_label(node)}" if charging else f"Standby at {_label(node)}"

    # ── leases (distributed mutex) ───────────────────────────────────────

    def _grant(self, zone: str, robot: RobotState, ticks: int, message: str) -> None:
        self._state.leases[zone] = robot.id
        self._lease_until[zone] = self._state.tick + ticks
        self._p2p(robot.id, "MESH", "MUTEX_GRANT", f"LEASE_ACQUIRED[{zone}, pri={robot.priority}, {ticks * CONTROL_PERIOD_SECONDS:.1f}s]")
        self._event("LEASE", message)

    def _release_lease(self, zone: str, robot_id: str, why: str) -> None:
        if self._state.leases.get(zone) == robot_id:
            del self._state.leases[zone]
            self._lease_until.pop(zone, None)
            self._p2p(robot_id, "MESH", "MUTEX_GRANT", f"RELEASE_MUTEX[{zone}]")
            self._event("LEASE", f"{robot_id} {why} {zone} & released distributed mutex")

    # ── waiting / deadlock resolution ────────────────────────────────────

    def _wait(self, robot: RobotState, node: str, holder: str, reason: str) -> None:
        count = self._waiting[robot.id] = self._waiting.get(robot.id, 0) + 1
        self._wait_for[robot.id], self._wait_node[robot.id] = holder, node
        here = _node_at(robot.position) if robot.progress == 0.0 else None
        if here is not None:  # a stationary robot only needs its own node; don't hold others' exits hostage
            for owned, owner in list(self._owner.items()):
                if owner == robot.id and owned != here:
                    del self._owner[owned]
        if robot.status != "Yielding":
            robot.status = "Yielding"
            self._p2p(robot.id, holder, "YIELD_ACK", f"YIELD[{reason}]")
            self._event("LEASE", f"{robot.id} yielding at safety line | {reason}")
        if count % DEADLOCK_TICKS != 0 or not robot.target:
            return
        cycle = self._deadlock_cycle(robot.id)
        if cycle:  # mutual wait: the lowest-ranked robot that can get out of the way does so, right now
            members = [self._robot(i) for i in cycle]
            movers = []
            for member in members:
                others = {_node_at(p) for o in members if o is not member for p in o.path[o.path_index:]}
                aside = self._side_step_node(member, others | {self._wait_node.get(member.id, "")})
                if aside is not None:
                    movers.append((member, aside))
            if movers:
                victim, aside = min(movers, key=lambda m: self._rank(m[0]))
                self._side_step(victim, aside, self._wait_for.get(victim.id, holder))
                return
        detour = self._plan(robot, robot.target, {node})
        if detour is not None:
            self._apply_path(robot, detour)
            robot.status = "Rerouting"
            self._waiting[robot.id] = 0
            self._wait_for.pop(robot.id, None)
            self._event("REROUTE", f"{robot.id} detoured around occupied {_label(node)}")

    def _deadlock_cycle(self, start: str) -> list[str] | None:
        chain: list[str] = []
        current = start
        while self._waiting.get(current, 0) > 0 and current in self._wait_for and current not in chain:
            chain.append(current)
            current = self._wait_for[current]
        return chain[chain.index(current):] if current in chain else None

    def _side_step_node(self, robot: RobotState, avoid: set[str | None]) -> str | None:
        """A free neighbour that is off every route the other robots in the deadlock need."""
        here = _node_at(robot.position) if robot.progress == 0.0 else None
        if here is None:
            return None
        free = [n for n in GRAPH_ADJACENCY[here] if n not in avoid and n not in self._owner and n not in self._blocked]
        return min(free, key=lambda n: math.hypot(WAREHOUSE_NODES[n].x - robot.position.x, WAREHOUSE_NODES[n].y - robot.position.y), default=None)

    def _side_step(self, robot: RobotState, node: str, other: str) -> None:
        self._apply_path(robot, [robot.position.model_copy()] + _points([node]))
        robot.status = "Rerouting"
        self._waiting[robot.id] = 0
        self._wait_for.pop(robot.id, None)
        self._hold[robot.id] = self._state.tick + HOLD_TICKS
        self._event("REROUTE", f"{robot.id} stepped aside to {_label(node)} to clear a deadlock with {other}")

    # ── movement ─────────────────────────────────────────────────────────

    def _hold_applies(self, robot: RobotState) -> bool:
        return robot.target is not None and _node_at(robot.position) != robot.target and robot.path_index == 0

    def _move(self, robot: RobotState) -> None:
        if robot.status == "Blocked":
            return
        if robot.path_index + 1 >= len(robot.path):
            if robot.leg != "idle":
                self._arrive(robot)
            return
        start, end = robot.path[robot.path_index], robot.path[robot.path_index + 1]
        if robot.progress == 0.0:
            if self._hold.get(robot.id, 0) > self._state.tick and self._hold_applies(robot):
                return
            here = _node_at(start)
            needed = [n for n in (_node_at(p) for p in robot.path[robot.path_index + 1: robot.path_index + 1 + LOOKAHEAD_NODES]) if n]
            if here and self._owner.get(here) not in (None, robot.id):
                return self._wait(robot, here, self._owner[here], f"{_label(here)} occupied by {self._owner[here]}")
            for node in needed:
                if self._owner.get(node) not in (None, robot.id):
                    return self._wait(robot, node, self._owner[node], f"{_label(node)} reserved by {self._owner[node]}")
                if node in MUTEX_ZONES and self._state.leases.get(node) not in (None, robot.id):
                    return self._wait(robot, node, self._state.leases[node], f"{node} held by {self._state.leases[node]}")
            for node in needed:
                self._owner[node] = robot.id
                if node in MUTEX_ZONES and node not in self._state.leases:
                    self._grant(node, robot, LEASE_TICKS, f"{robot.id} leased {node} for {LEASE_TICKS * CONTROL_PERIOD_SECONDS:.1f}s | priority {robot.priority}")
            if here:
                if self._owner.get(here) == robot.id:
                    del self._owner[here]
                if here in MUTEX_ZONES:
                    self._release_lease(here, robot.id, "cleared")
        self._waiting[robot.id] = 0
        self._wait_for.pop(robot.id, None)

        distance = math.hypot(end.x - start.x, end.y - start.y) or 1.0
        progress = robot.progress + self._fleet[robot.id].speed / distance
        robot.battery = max(0.0, robot.battery - DRAIN_PER_TICK)
        if robot.status != "Rerouting":
            robot.status = "Moving"
        if progress >= 1.0:
            robot.path_index += 1
            robot.progress = 0.0
            robot.position = end.model_copy()
            self._prune_reservations(robot)
            if robot.status == "Rerouting":
                robot.status = "Moving"
        else:
            robot.progress = progress
            robot.position = Point(x=start.x + (end.x - start.x) * progress, y=start.y + (end.y - start.y) * progress)

    def _arrive(self, robot: RobotState) -> None:
        if robot.target and _node_at(robot.position) != robot.target:
            self._route_leg(robot)  # ended a side-step / partial path: continue to the real target
            return
        task = self._tasks.get(robot.task_id) if robot.task_id else None
        if robot.leg in ("to_pickup", "to_drop") and (task is None or task.assigned_robot_id != robot.id):
            self._free(robot)  # task was withdrawn while en route
        elif robot.leg == "to_pickup":
            robot.current_payload_kg = task.payload_kg
            robot.leg, robot.target = "to_drop", task.destination
            self._set_task(task, status="In Progress")
            self._event("HANDOFF", f"{robot.id} loaded {task.payload_kg:g}kg at {_label(task.pickup)} for {task.id}")
            self._route_leg(robot)
        elif robot.leg == "to_drop":
            self._complete(task)
            robot.completed += 1
            self._event("HANDOFF", f"{task.id} delivered to {_label(task.destination)} by {robot.id}")
            robot.task_id, robot.current_payload_kg, robot.priority = None, 0.0, BASE_PRIORITY
            robot.leg, robot.target = "idle", None
            self._send_idle(robot)
        else:
            self._dock(robot)

    # ── simulation step ──────────────────────────────────────────────────

    def _step(self) -> None:
        s = self._state
        s.tick += 1

        for zone, until in list(self._lease_until.items()):
            holder = s.leases.get(zone)
            if holder and s.tick >= until and self._owner.get(zone) != holder:
                self._release_lease(zone, holder, "lease expired on")

        for robot in s.robots:
            if robot.status == "Blocked" and robot.target and s.tick % 3 == 0:
                self._route_leg(robot, announce=False)
        self._auction()
        for robot in s.robots:
            if robot.leg == "idle" and robot.status != "Blocked" and self._docked(robot):
                self._send_idle(robot)

        for robot in sorted(s.robots, key=self._rank, reverse=True):
            self._move(robot)

        for robot in s.robots:
            if self._docked(robot) and _node_at(robot.position) == CHARGE_NODE and robot.battery < 100.0:
                robot.battery = min(100.0, robot.battery + CHARGE_RATE)
                robot.status = "Charging" if robot.battery < CHARGED else "Idle"

        self._check_collisions()
        if s.tick % HEARTBEAT_EVERY == 0:
            for robot in s.robots:
                node = _node_at(robot.position) or find_closest_node(robot.position.x, robot.position.y)
                self._p2p(robot.id, "MESH", "HEARTBEAT", f"PEER_SYNC[battery={robot.battery:.0f}%, node={node}]")

    def _check_collisions(self) -> None:
        # Robots waiting at a node they do not own (or docked) are not on the lane yet.
        active = [
            r for r in self._state.robots
            if r.progress > 0 or (node := _node_at(r.position)) is None or self._owner.get(node) == r.id
        ]
        touching = set()
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if math.hypot(a.position.x - b.position.x, a.position.y - b.position.y) < COLLISION_RADIUS:
                    touching.add(frozenset((a.id, b.id)))
        for pair in touching - self._collided:
            self._state.collision_count += 1
            self._event("REROUTE", f"Proximity violation: {' & '.join(sorted(pair))}")
        self._collided = touching
