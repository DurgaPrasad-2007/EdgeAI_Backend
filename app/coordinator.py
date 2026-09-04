from __future__ import annotations

import asyncio
from contextlib import suppress

from app.schemas import (
    BlockageRequest,
    FleetEvent,
    FleetState,
    IntentRequest,
    Point,
    ReservationRequest,
    RobotId,
    RobotState,
    SimulationControl,
    TaskCreate,
    TaskBidRequest,
    TaskRecord,
)
from app.task_store import TaskStore
from app.database import Database
from app.cache import CacheManager

CONTROL_PERIOD_SECONDS = 0.6
CONFLICT_POINT = Point(x=500, y=270)

ROUTES: dict[RobotId, list[Point]] = {
    "AMR-01": [Point(x=102, y=270), Point(x=350, y=270), CONFLICT_POINT, Point(x=720, y=270), Point(x=900, y=270)],
    "AMR-02": [Point(x=500, y=85), Point(x=500, y=180), CONFLICT_POINT, Point(x=500, y=444), Point(x=500, y=540)],
    "AMR-03": [Point(x=900, y=453), Point(x=720, y=453), Point(x=650, y=390), Point(x=500, y=390), Point(x=280, y=390), Point(x=102, y=390)],
}

DETOUR = [Point(x=900, y=453), Point(x=720, y=453), Point(x=720, y=505), Point(x=280, y=505), Point(x=102, y=390)]


def _copy_points(points: list[Point]) -> list[Point]:
    return [point.model_copy(deep=True) for point in points]


def _event(tick: int, event_type: FleetEvent.type, message: str) -> FleetEvent:
    return FleetEvent(time=f"T+{tick * CONTROL_PERIOD_SECONDS:04.1f}s", type=event_type, message=message)


def initial_state() -> FleetState:
    robots = [
        RobotState(id="AMR-01", name="Atlas", color="#7aefc6", battery=82, status="Moving", task="Pick P-17 -> Dock E", priority=71, path=_copy_points(ROUTES["AMR-01"]), path_index=0, progress=0, position=ROUTES["AMR-01"][0].model_copy(), completed=12),
        RobotState(id="AMR-02", name="Nova", color="#ffcb6b", battery=48, status="Moving", task="Replenish R-04", priority=91, path=_copy_points(ROUTES["AMR-02"]), path_index=0, progress=0, position=ROUTES["AMR-02"][0].model_copy(), completed=10),
        RobotState(id="AMR-03", name="Kite", color="#92b8ff", battery=67, status="Moving", task="Pick P-23 -> Dock W", priority=63, path=_copy_points(ROUTES["AMR-03"]), path_index=0, progress=0, position=ROUTES["AMR-03"][0].model_copy(), completed=11),
    ]
    return FleetState(
        tick=0,
        running=False,
        aisle_blocked=False,
        reservation=None,
        lease_until=0,
        completed_tasks=33,
        collision_count=0,
        messages=0,
        events=[
            _event(0, "HEARTBEAT", "Mesh online | 3 peers discovered | direct local links healthy"),
            _event(0, "INTENT", "AMR-01 published corridor C-14 intent (ETA 6.4 s)"),
        ],
        robots=robots,
    )


class FleetCoordinator:
    """Simulation twin that records peer-agent decisions and exposes read-only telemetry.

    The API does not issue wheel commands. On real hardware each AMR runs the same
    intent/lease state machine, then publishes telemetry through a local bridge.
    """

    def __init__(self, database: Database, cache: CacheManager | None = None) -> None:
        self._state = initial_state()
        self._cache = cache
        self._task_store = TaskStore(database.sessions, database.url, cache=cache)
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[FleetState]] = set()
        self._ticker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._ticker = asyncio.create_task(self._tick_loop(), name="edgefleet-simulation-ticker")

    async def stop(self) -> None:
        if self._ticker is None:
            return
        self._ticker.cancel()
        with suppress(asyncio.CancelledError):
            await self._ticker
        self._ticker = None

    async def snapshot(self) -> FleetState:
        async with self._lock:
            return self._state.model_copy(deep=True)

    async def reset(self) -> FleetState:
        async with self._lock:
            self._state = initial_state()
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return snapshot

    async def set_simulation(self, control: SimulationControl) -> FleetState:
        async with self._lock:
            self._state.running = control.running
            label = "resumed" if control.running else "paused"
            self._append_event("HEARTBEAT", f"Local twin {label}; peer agents retain independent safety state")
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return snapshot

    async def report_intent(self, intent: IntentRequest) -> FleetState:
        async with self._lock:
            self._require_robot(intent.robot_id)
            self._state.messages += 1
            self._append_event("INTENT", f"{intent.robot_id} published {intent.corridor_id} intent (ETA {intent.eta_seconds:.1f} s)")
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return snapshot

    async def request_reservation(self, request: ReservationRequest) -> FleetState:
        async with self._lock:
            robot = self._require_robot(request.robot_id)
            if request.corridor_id != "C-14":
                self._append_event("LEASE", f"{request.robot_id} local lease for {request.corridor_id} acknowledged")
            elif self._state.reservation in (None, request.robot_id):
                self._state.reservation = request.robot_id
                self._state.lease_until = self._state.tick + max(1, round(request.lease_seconds / CONTROL_PERIOD_SECONDS))
                self._append_event("LEASE", f"{robot.id} leased C-14 for {request.lease_seconds:.1f} s | priority {robot.priority}")
            else:
                holder = self._require_robot(self._state.reservation)
                winner = max((holder, robot), key=self._priority_score)
                if winner.id == robot.id:
                    self._state.reservation = robot.id
                    self._state.lease_until = self._state.tick + max(1, round(request.lease_seconds / CONTROL_PERIOD_SECONDS))
                    self._append_event("LEASE", f"{robot.id} preempted C-14 lease after deterministic peer scoring")
                else:
                    robot.status = "Yielding"
                    self._append_event("LEASE", f"{robot.id} yielded C-14 to {holder.id}; safety buffer maintained")
            self._state.messages += 1
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return snapshot

    async def report_blockage(self, request: BlockageRequest) -> FleetState:
        async with self._lock:
            if request.aisle_id == "B-07":
                self._apply_blockage()
            else:
                self._state.messages += 1
                self._append_event("REROUTE", f"Peer mesh recorded blockage {request.aisle_id}; no route in this floor twin uses it")
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return snapshot

    async def bid_for_task(self, request: TaskBidRequest) -> FleetState:
        async with self._lock:
            candidates = [self._require_robot(robot_id) for robot_id in request.candidate_robot_ids]
            eligible = [robot for robot in candidates if robot.status != "Charging"]
            winner = max(eligible or candidates, key=self._priority_score)
            winner.task = f"{request.task_id} -> {request.destination}"
            winner.status = "Task handoff"
            winner.priority = min(100, winner.priority + 8)
            self._state.messages += len(candidates)
            self._append_event("HANDOFF", f"{winner.id} won {request.task_id} bid for {request.destination}")
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return snapshot

    async def create_task(self, request: TaskCreate) -> TaskRecord:
        async with self._lock:
            candidates = [robot for robot in self._state.robots if robot.status != "Charging"]
            winner = max(candidates, key=lambda robot: self._priority_score(robot) + request.priority * 0.2) if candidates else None
            task = await self._task_store.create(request.pickup, request.destination, request.priority, winner.id if winner else None)
            if winner:
                winner.task = f"{task.id}: {request.pickup} -> {request.destination}"
                winner.status = "Task handoff"
                winner.priority = min(100, max(winner.priority, request.priority))
                self._state.messages += len(candidates)
                self._append_event("HANDOFF", f"{winner.id} won {task.id} by local task-cost bidding")
            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return task

    async def list_tasks(self) -> list[TaskRecord]:
        async with self._lock:
            return await self._task_store.list()

    async def complete_task(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            task = await self._task_store.complete(task_id)
            if task is not None:
                self._state.completed_tasks += 1
                self._append_event("HEARTBEAT", f"{task.id} completed locally by {task.assigned_robot_id or 'unassigned task queue'}")
                snapshot = self._state.model_copy(deep=True)
            else:
                snapshot = None
        if snapshot is not None:
            await self._broadcast(snapshot)
        return task

    async def add_knowledge(self, item):
        return await self._task_store.add_knowledge(item)

    async def search_knowledge(self, embedding: list[float], limit: int):
        return await self._task_store.search_knowledge(embedding, limit)

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
            async with self._lock:
                if not self._state.running:
                    continue
                self._step()
                snapshot = self._state.model_copy(deep=True)
            await self._broadcast(snapshot)

    async def _broadcast(self, snapshot: FleetState) -> None:
        if self._cache:
            with suppress(Exception):
                await self._cache.set_json("edgefleet:state:latest", snapshot.model_dump(mode="json"), ttl=10)
        for queue in tuple(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(snapshot)

    def _step(self) -> None:
        self._state.tick += 1
        self._state.messages += 3

        if self._state.reservation and (
            self._state.tick >= self._state.lease_until
            or self._has_cleared_conflict(self._require_robot(self._state.reservation))
        ):
            holder = self._state.reservation
            self._state.reservation = None
            self._state.lease_until = 0
            self._append_event("LEASE", f"{holder} released C-14 reservation")

        contenders = sorted(
            (robot for robot in self._state.robots if self._is_approaching_conflict(robot)),
            key=self._priority_score,
            reverse=True,
        )
        if self._state.reservation is None and contenders:
            winner = contenders[0]
            self._state.reservation = winner.id
            self._state.lease_until = self._state.tick + 8
            self._append_event("LEASE", f"{winner.id} leased C-14 for 4.8 s | priority {winner.priority}")

        for robot in self._state.robots:
            if self._is_approaching_conflict(robot) and self._state.reservation != robot.id:
                robot.status = "Yielding"
            else:
                self._move(robot, 25 if robot.id == "AMR-02" else 22)

        if self._state.tick % 8 == 0:
            self._append_event("HEARTBEAT", "Peer heartbeat quorum 3/3 | state convergence 84 ms")
        else:
            self._append_event("INTENT", "Trajectory and occupancy intent replicated across peer mesh")

        if self._state.tick == 14 and not self._state.aisle_blocked:
            self._apply_blockage()

    def _apply_blockage(self) -> None:
        if self._state.aisle_blocked:
            return
        r03 = self._require_robot("AMR-03")
        r03.path = _copy_points(DETOUR)
        r03.path_index = 1
        r03.progress = 0
        r03.position = DETOUR[1].model_copy()
        r03.status = "Rerouting"
        r03.task = "Reroute around B-07"

        r01 = self._require_robot("AMR-01")
        r01.task = "Pick P-17 + P-23 -> Dock E"
        r01.status = "Task handoff"
        r01.priority = 84

        self._state.aisle_blocked = True
        self._state.messages += 6
        self._append_event("HANDOFF", "AMR-01 won P-23 bid after AMR-03 reported B-07 blocked")
        self._append_event("REROUTE", "AMR-03 published B-07 blockage; local detour lease accepted")

    def _move(self, robot: RobotState, speed: float) -> None:
        next_index = robot.path_index + 1
        if next_index >= len(robot.path):
            robot.status = "Charging"
            robot.battery = min(100, robot.battery + 0.1)
            return

        start = robot.path[robot.path_index]
        end = robot.path[next_index]
        distance = max(1, ((end.x - start.x) ** 2 + (end.y - start.y) ** 2) ** 0.5)
        progress = robot.progress + speed / distance
        robot.status = "Moving"
        robot.battery = max(0, robot.battery - (0.13 if progress >= 1 else 0.07))

        if progress >= 1:
            robot.path_index = next_index
            robot.progress = 0
            robot.position = end.model_copy()
            return

        robot.progress = progress
        robot.position = Point(x=start.x + (end.x - start.x) * progress, y=start.y + (end.y - start.y) * progress)

    def _is_approaching_conflict(self, robot: RobotState) -> bool:
        if robot.path_index + 1 >= len(robot.path):
            return False
        next_point = robot.path[robot.path_index + 1]
        return next_point.x == CONFLICT_POINT.x and next_point.y == CONFLICT_POINT.y and robot.progress > 0.55

    @staticmethod
    def _has_cleared_conflict(robot: RobotState) -> bool:
        return robot.path_index > 2

    @staticmethod
    def _priority_score(robot: RobotState) -> float:
        return robot.priority + (100 - robot.battery) * 0.2

    def _require_robot(self, robot_id: RobotId) -> RobotState:
        for robot in self._state.robots:
            if robot.id == robot_id:
                return robot
        raise ValueError(f"Unknown robot {robot_id}")

    def _append_event(self, event_type: FleetEvent.type, message: str) -> None:
        self._state.events.insert(0, _event(self._state.tick, event_type, message))
        del self._state.events[7:]
