"""Fleet gateway: the API/persistence/streaming edge of the peer mesh.

The robots coordinate themselves (see `agents.py`). This class does not plan paths, grant leases, run auctions
or move anyone: it (1) accepts work orders and operator/fault-injection commands and turns them into mesh
messages, (2) persists the task board and audit trail, (3) fans the observer snapshot out to dashboards, and
(4) in live mode runs each robot's own asyncio control loop. If it stopped, robots that are already moving
would keep negotiating with each other.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone

from app.agents import (
    COLLISION_RADIUS,
    CONTROL_PERIOD_SECONDS,
    LEASE_TICKS,
    LOW_BATTERY,
    MIN_BID_BATTERY,
    Msg,
    label,
)
from app.audit import SYSTEM_ACTOR, actor_var
from app.cache import CacheManager
from app.database import Database
from app.graph import MUTEX_ZONES, ROBOT_FLEET, RobotSpec, resolve_node, world_payload
from app.schemas import (
    AuditRecord,
    BlockageRequest,
    FleetState,
    IntentRequest,
    ReservationRequest,
    SimulationControl,
    TaskBidRequest,
    TaskCreate,
    TaskRecord,
    TaskUpdate,
)
from app.sim import FleetSim, _aware
from app.task_store import TaskStore

log = logging.getLogger("edgefleet.coordinator")

__all__ = ["FleetCoordinator", "FleetError", "FleetConflict", "CONTROL_PERIOD_SECONDS", "COLLISION_RADIUS", "LOW_BATTERY", "MIN_BID_BATTERY"]


class FleetError(ValueError):
    """Invalid request (unknown robot / location / task)."""


class FleetConflict(FleetError):
    """Request is valid but conflicts with current fleet state."""


class FleetCoordinator:
    def __init__(self, database: Database, cache: CacheManager | None = None, fleet: tuple[RobotSpec, ...] = ROBOT_FLEET, policy: str = "decentralized") -> None:
        self._fleet_specs = fleet
        self._cache = cache
        self._task_store = TaskStore(database.sessions, database.url, cache=cache)
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[FleetState]] = set()
        self._pending: list[tuple] = []  # write-behind queue: ("create", task) | ("update", id, fields) | ("delete", id)
        self._flush_task: asyncio.Task[None] | None = None
        self._pending_audit: list[dict] = []
        self._sim = FleetSim(fleet, policy, on_task=self._queue_task, on_event=self._audit_event)
        self._live = False
        self._clock_task: asyncio.Task[None] | None = None
        self._agent_tasks: list[asyncio.Task[None]] = []

    @property
    def _tasks(self) -> dict[str, TaskRecord]:
        return self._sim.tasks

    # ── persistence hooks (the sim reports what changed; we store it) ────

    def _queue_task(self, task: TaskRecord, fields: dict) -> None:
        self._pending.append(("update", task.id, fields))

    def _audit_event(self, type_: str, message: str, stamp: str) -> None:
        actor = actor_var.get()
        category = "TASK" if type_ == "HANDOFF" else "SYSTEM" if actor == SYSTEM_ACTOR else "CONTROL"
        self._pending_audit.append({"actor": actor, "category": category, "event_type": type_, "sim_time": stamp, "message": message})
        log.info("[%s] [%s] %s", stamp, type_, message.replace("\u2192", "->"))  # console/file log encodings may not hold arrows

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, ticker: bool = True) -> None:
        sim = self._sim
        await self._task_store.requeue_orphans()
        for task in await self._task_store.list():
            task.created_at = _aware(task.created_at)
            sim.add_task(task)
            # Rows written before locations were validated: canonicalise what we can ("Dock E" -> DOCK-E).
            pickup, destination = resolve_node(task.pickup), resolve_node(task.destination)
            if pickup and destination and (pickup, destination) != (task.pickup, task.destination):
                sim._set_task(task, pickup=pickup, destination=destination)
        sim.completed_tasks = sum(1 for t in sim.tasks.values() if t.status == "Completed")
        sim.dispatch()
        await self._flush()
        if ticker:
            self._live = True
            self._spawn_live()

    async def stop(self) -> None:
        self._live = False
        await self._stop_live()
        await self._flush()  # write-behind: nothing may be left unsaved on shutdown

    def _spawn_live(self) -> None:
        """Live mode: one clock, plus for every robot an independent control loop and a mesh listener."""
        sim = self._sim
        self._clock_task = asyncio.create_task(self._clock_loop(), name="edgefleet-clock")
        n = max(1, len(sim.agents))
        for i, agent in enumerate(sim.agents):
            phase = CONTROL_PERIOD_SECONDS * i / n  # robots are not synchronised with each other
            self._agent_tasks.append(asyncio.create_task(agent.run(lambda: sim.running, phase), name=f"{agent.id}-control"))
            self._agent_tasks.append(asyncio.create_task(agent.listen(), name=f"{agent.id}-mesh"))

    async def _stop_live(self) -> None:
        tasks = [t for t in (self._clock_task, *self._agent_tasks) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._clock_task, self._agent_tasks = None, []

    async def _clock_loop(self) -> None:
        sim = self._sim
        while True:
            await asyncio.sleep(CONTROL_PERIOD_SECONDS)
            try:
                if not sim.running:
                    continue
                sim.clock.tick += 1
                sim.dispatch()
                sim.world.check_collisions(sim.event)
                await self._publish(sim.snapshot(), flush_in_background=True)
            except Exception:  # keep the fleet alive; the next tick retries
                log.exception("clock tick failed")

    def _step(self) -> None:
        """Advance the whole mesh one control tick (manual clock: tests and headless runs)."""
        self._sim.step()

    def world(self) -> dict:
        payload = world_payload(self._fleet_specs)
        payload["config"] = {
            "control_period_s": CONTROL_PERIOD_SECONDS,
            "low_battery_pct": LOW_BATTERY,
            "min_bid_battery_pct": MIN_BID_BATTERY,
            "collision_radius": COLLISION_RADIUS,
            "lease_ttl_s": round(LEASE_TICKS * CONTROL_PERIOD_SECONDS, 1),
            "architecture": "decentralized peer mesh",
        }
        return payload

    async def snapshot(self) -> FleetState:
        async with self._lock:
            return self._sim.snapshot(bump=False)

    async def reset(self, clear_tasks: bool = False) -> FleetState:
        """Robots return to their docks and obstacles clear. Unfinished jobs are re-queued, or dropped when `clear_tasks`."""
        async with self._lock:
            if clear_tasks:  # a fresh floor: nothing left over from the previous demo scenario
                for task_id in [t.id for t in self._tasks.values() if t.status != "Completed"]:
                    del self._tasks[task_id]
                    self._pending.append(("delete", task_id))
            if self._live:
                await self._stop_live()
            self._sim.reset()
            if self._live:
                self._spawn_live()
            snapshot = self._sim.snapshot()
        await self._publish(snapshot)
        return snapshot

    async def set_simulation(self, control: SimulationControl) -> FleetState:
        async with self._lock:
            self._sim.running = control.running
            self._sim.event("HEARTBEAT", f"Simulation {'resumed' if control.running else 'paused'}")
            snapshot = self._sim.snapshot()
        await self._publish(snapshot)
        return snapshot

    async def compare_policies(self) -> dict:
        """Measured decentralised-vs-stop-and-wait comparison (headless, deterministic, cached)."""
        from app.benchmark import compare

        return await asyncio.to_thread(compare)

    # ── peer protocol API: operator commands become mesh messages ────────

    def _agent(self, robot_id: str):
        agent = self._sim.agent(robot_id)
        if agent is None:
            raise FleetError(f"Unknown robot {robot_id}")
        return agent

    async def _done(self) -> FleetState:
        self._sim.bus.pump()
        snapshot = self._sim.snapshot()
        await self._publish(snapshot)
        return snapshot

    async def report_intent(self, intent: IntentRequest) -> FleetState:
        async with self._lock:
            agent = self._agent(intent.robot_id)
            zone = resolve_node(intent.corridor_id) or intent.corridor_id
            agent.publish_intent(zone, intent.eta_seconds)
            self._sim.event("INTENT", f"{intent.robot_id} published {intent.corridor_id} intent (ETA {intent.eta_seconds:.1f} s)")
            return await self._done()

    async def request_reservation(self, request: ReservationRequest) -> FleetState:
        async with self._lock:
            agent = self._agent(request.robot_id)
            zone = resolve_node(request.corridor_id)
            ticks = max(1, round(request.lease_seconds / CONTROL_PERIOD_SECONDS))
            if zone not in MUTEX_ZONES:
                self._sim.event("LEASE", f"{agent.id} local lease for {request.corridor_id} acknowledged (not a mutex zone)")
            elif not agent.request_zone(zone, ticks):
                self._sim.event("LEASE", f"{agent.id} yielded {zone}: a live peer already holds it; safety buffer maintained")
            return await self._done()

    async def report_blockage(self, request: BlockageRequest) -> FleetState:
        async with self._lock:
            node = resolve_node(request.aisle_id)
            if node is None:
                raise FleetError(f"Unknown aisle/node '{request.aisle_id}'")
            self._sim.report_blockage(node, request.blocked)
            self._sim.dispatch()
            return await self._done()

    async def set_robot_battery(self, robot_id: str, battery: float) -> FleetState:
        async with self._lock:
            self._agent(robot_id).set_battery(battery)
            return await self._done()

    async def simulate_agent_failure(self, robot_id: str) -> FleetState:
        async with self._lock:
            self._agent(robot_id).go_silent()
            return await self._done()

    async def bid_for_task(self, request: TaskBidRequest) -> FleetState:
        async with self._lock:
            task = self._tasks.get(request.task_id)
            if task is None:
                raise FleetError(f"Unknown task {request.task_id}")
            if resolve_node(request.destination) != task.destination:
                raise FleetConflict(f"Destination does not match task {task.id}")
            if task.status not in ("Queued", "Blocked"):
                raise FleetConflict(f"Task {task.id} is {task.status}; only queued tasks can be auctioned")
            for rid in request.candidate_robot_ids:
                self._agent(rid)
            self._sim.announce(task, only=list(request.candidate_robot_ids))
            if task.status != "Assigned":
                raise FleetConflict(f"No eligible candidate for {task.id}")
            return await self._done()

    # ── task API ─────────────────────────────────────────────────────────

    async def create_task(self, request: TaskCreate) -> TaskRecord:
        pickup, destination = resolve_node(request.pickup), resolve_node(request.destination)
        if pickup is None or destination is None:
            raise FleetError(f"Unknown location: {request.pickup if pickup is None else request.destination}")
        if pickup == destination:
            raise FleetError("Pickup and destination must differ")
        async with self._lock:
            # In-memory first and answer immediately; the database write happens behind the request.
            task = TaskRecord(
                id=self._task_store.new_id(), pickup=pickup, destination=destination, priority=request.priority, status="Queued",
                payload_kg=request.payload_kg, payload_size=request.payload_size, urgency=request.urgency,
                assigned_robot_id=None, created_at=datetime.now(timezone.utc),
            )
            self._pending.append(("create", task.model_copy()))
            self._sim.add_task(task)
            self._sim.event("HANDOFF", f"{task.id} queued: {label(pickup)} → {label(destination)} ({task.payload_kg:g}kg {task.payload_size})")
            self._sim.dispatch()
            result = task.model_copy()
            await self._done()
        return result

    def _revoke(self, task: TaskRecord) -> None:
        """Tell the mesh a task is withdrawn; whichever robot holds it drops it."""
        self._sim.bus.publish(Msg("TASK_REVOKE", "WMS", "MESH", {"task_id": task.id, "robot": task.assigned_robot_id}, self._sim.clock.tick))
        self._sim.bus.pump()

    def _apply_update(self, task: TaskRecord, request: TaskUpdate) -> None:
        sim = self._sim
        if task.status == "Completed" and (request.status not in (None, "Completed") or request.assigned_robot_id):
            raise FleetConflict(f"Task {task.id} is already completed")
        owner = sim.agent(task.assigned_robot_id) if task.assigned_robot_id else None
        if request.payload_kg is not None and owner and request.payload_kg > owner.spec.max_payload:
            raise FleetConflict(f"{owner.id} cannot carry {request.payload_kg:g}kg")
        changes = {k: v for k, v in request.model_dump(exclude={"status", "assigned_robot_id"}).items() if v is not None}
        if changes:
            sim._set_task(task, **changes)
            sim.bus.publish(Msg("TASK_UPDATE", "WMS", "MESH", {"task_id": task.id, **changes}, sim.clock.tick))
            sim.event("HANDOFF", f"Task {task.id} attributes reconfigured ({', '.join(f'{k}={v}' for k, v in changes.items())})")

        if request.assigned_robot_id and request.assigned_robot_id != task.assigned_robot_id:
            target = self._agent(request.assigned_robot_id)
            payload = sim._payload(task)
            if not target.can_take(payload):
                raise FleetConflict(f"{target.id} cannot take {task.id} right now")
            if owner:
                self._revoke(task)
            sim.bus.publish(Msg("TASK_DIRECTIVE", "WMS", target.id, {"robot": target.id, "task": payload}, sim.clock.tick))
            sim.bus.pump()
            sim.event("HANDOFF", f"{task.id} manually reassigned to {target.id}")
        if request.status and request.status != task.status:
            if request.status == "Completed":
                self._revoke(task)
                if task.status != "Completed":
                    sim._set_task(task, status="Completed")
                    sim.completed_tasks += 1
                    sim.session_completed += 1
            elif request.status in ("Queued", "Blocked"):
                self._revoke(task)
                sim._set_task(task, status=request.status, unassign=True)
                sim.announced.pop(task.id, None)
            else:
                raise FleetConflict(f"Status {request.status} is set by the fleet, not by clients")

    async def update_task(self, task_id: str, request: TaskUpdate) -> TaskRecord | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            self._apply_update(task, request)
            self._sim.dispatch()
            result = task.model_copy()
            await self._done()
        return result

    async def complete_task(self, task_id: str) -> TaskRecord | None:
        return await self.update_task(task_id, TaskUpdate(status="Completed"))

    async def delete_task(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            self._revoke(task)
            del self._tasks[task_id]
            self._pending.append(("delete", task_id))
            self._sim.event("HANDOFF", f"Task {task_id} withdrawn from auction queue")
            self._sim.dispatch()
            await self._done()
        return True

    async def list_tasks(self) -> list[TaskRecord]:
        async with self._lock:
            return [t.model_copy() for t in sorted(self._tasks.values(), key=lambda t: _aware(t.created_at), reverse=True)]

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

    # ── pub/sub to dashboards ────────────────────────────────────────────

    async def subscribe(self) -> asyncio.Queue[FleetState]:
        queue: asyncio.Queue[FleetState] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[FleetState]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def _schedule_flush(self) -> None:
        if (self._pending or self._pending_audit) and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = asyncio.create_task(self._flush(), name="edgefleet-write-behind")

    async def _publish(self, snapshot: FleetState, flush_in_background: bool = True) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(snapshot)
        if self._cache:
            with suppress(Exception):
                await self._cache.set_json("edgefleet:state:latest", snapshot.model_dump(mode="json"), ttl=10)
        if flush_in_background:
            self._schedule_flush()
        else:
            await self._flush()

    async def audit(self, category: str, message: str, actor: str | None = None) -> None:
        """Record an event that is not part of the simulation (logins, admin changes)."""
        act = actor or actor_var.get()
        log.info("[AUDIT] [%s] actor=%s - %s", category, act, message)
        self._pending_audit.append({"actor": act, "category": category, "message": message})
        await self._flush()

    async def list_audit(self, limit: int = 200, category: str | None = None) -> list[AuditRecord]:
        await self._flush()
        return await self._task_store.list_audit(limit, category)

    async def _flush(self) -> None:
        """Persist task transitions and audit rows in order, outside the state lock."""
        async with self._flush_lock:
            while self._pending_audit or self._pending:
                rows, self._pending_audit = self._pending_audit, []
                ops, self._pending = self._pending, []
                try:
                    await self._task_store.commit(rows, ops)  # one transaction = one connection, however many changes
                except Exception:
                    log.exception("batch persist failed (%d rows, %d task ops); retrying separately", len(rows), len(ops))
                    for chunk_rows, chunk_ops in [(rows, [])] + [([], [op]) for op in ops]:
                        try:
                            await self._task_store.commit(chunk_rows, chunk_ops)
                        except Exception:
                            log.exception("failed to persist %s", chunk_ops[0][:2] if chunk_ops else f"{len(chunk_rows)} audit rows")
