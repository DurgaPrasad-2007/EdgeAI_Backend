from __future__ import annotations

import asyncio
import math
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
    TaskUpdate,
    TaskBidRequest,
    TaskRecord,
)
from app.task_store import TaskStore
from app.database import Database
from app.cache import CacheManager
from app.graph import WAREHOUSE_NODES, find_shortest_path, find_closest_node

CONTROL_PERIOD_SECONDS = 0.6
CONFLICT_POINT = Point(x=500, y=270)

# Physical Robot Specifications
ROBOT_PROFILES = {
    "AMR-01": {"name": "Atlas", "color": "#C2541A", "max_payload": 1200.0, "speed": 20.0, "start_node": "DOCK-W"},
    "AMR-02": {"name": "Nova", "color": "#F59E0B", "max_payload": 350.0, "speed": 24.0, "start_node": "INT-N1"},
    "AMR-03": {"name": "Kite", "color": "#38BDF8", "max_payload": 700.0, "speed": 20.0, "start_node": "DOCK-E"},
}


def _event(tick: int, event_type: FleetEvent.type, message: str) -> FleetEvent:
    return FleetEvent(time=f"T+{tick * CONTROL_PERIOD_SECONDS:04.1f}s", type=event_type, message=message)


def _to_points(coords: list[tuple[float, float]]) -> list[Point]:
    return [Point(x=x, y=y) for x, y in coords]


def initial_state() -> FleetState:
    # Generate algorithmic initial paths using A*
    p1 = _to_points(find_shortest_path("DOCK-W", "DOCK-E"))
    p2 = _to_points(find_shortest_path("INT-N1", "CHARGE"))
    p3 = _to_points(find_shortest_path("DOCK-E", "BYPASS-W"))

    robots = [
        RobotState(
            id="AMR-01",
            name="Atlas",
            color="#C2541A",
            battery=96.0,
            status="Idle",
            task="Standby at Dock W",
            priority=50,
            path=p1,
            path_index=0,
            progress=0.0,
            position=p1[0].model_copy(),
            completed=0,
        ),
        RobotState(
            id="AMR-02",
            name="Nova",
            color="#F59E0B",
            battery=90.0,
            status="Idle",
            task="Standby at Bay North",
            priority=50,
            path=p2,
            path_index=0,
            progress=0.0,
            position=p2[0].model_copy(),
            completed=0,
        ),
        RobotState(
            id="AMR-03",
            name="Kite",
            color="#38BDF8",
            battery=94.0,
            status="Idle",
            task="Standby at Dock E",
            priority=50,
            path=p3,
            path_index=0,
            progress=0.0,
            position=p3[0].model_copy(),
            completed=0,
        ),
    ]

    return FleetState(
        tick=0,
        running=False,
        aisle_blocked=False,
        reservation=None,
        lease_until=0,
        completed_tasks=0,
        collision_count=0,
        messages=0,
        events=[
            _event(0, "HEARTBEAT", "Zenoh P2P Mesh online | 3 peers discovered | direct zero-broker links active"),
            _event(0, "INTENT", "AMR-01 published Corridor C-14 traversal intent (ETA 5.8s, Pri=75)"),
        ],
        robots=robots,
    )


class FleetCoordinator:
    """Algorithmic multi-agent simulation twin that calculates real A* trajectories,
    Contract-Net Protocol auctions, and distributed corridor mutex arbitration.
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
            self._append_event("HEARTBEAT", f"Simulation {label} · Dynamic peer arbitration loop active")
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
            if self._state.aisle_blocked:
                # Clear obstacle & restore optimal A* paths
                self._state.aisle_blocked = False
                for robot in self._state.robots:
                    curr_node = find_closest_node(robot.position.x, robot.position.y)
                    target_node = "DOCK-E" if robot.id == "AMR-01" else "DOCK-W"
                    coords = find_shortest_path(curr_node, target_node)
                    robot.path = _to_points(coords)
                    robot.path_index = 0
                    robot.progress = 0.0
                    robot.status = "Moving"
                self._append_event("REROUTE", f"Obstacle cleared in Aisle {request.aisle_id} · Restoring optimal A* paths")
            else:
                # Inject obstacle: Aisle B-07 impassable, recompute dynamic D* Lite detour
                self._state.aisle_blocked = True
                blocked = {"AISLE-B07"}
                for robot in self._state.robots:
                    curr_node = find_closest_node(robot.position.x, robot.position.y)
                    coords = find_shortest_path(curr_node, "DOCK-W", blocked)
                    robot.path = _to_points(coords)
                    robot.path_index = 0
                    robot.progress = 0.0
                    if robot.id == "AMR-03":
                        robot.status = "Rerouting"
                        robot.task = "D* Detour around B-07"
                    elif robot.id == "AMR-01":
                        robot.task = "Pick P-17 + P-23 -> Dock E"
                        robot.status = "Task handoff"
                        robot.priority = 84
                self._append_event("REROUTE", f"Obstacle detected in Aisle {request.aisle_id} · Dynamic D* Lite reroute computed")
                self._append_event("HANDOFF", "AMR-01 won P-23 bid after AMR-03 reported B-07 blocked")

            self._state.messages += 4
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
            # Algorithmic Contract-Net Protocol (CNP) bidding
            # Filter AMRs capable of handling payload weight
            eligible: list[tuple[RobotState, float]] = []
            for robot in self._state.robots:
                max_cap = ROBOT_PROFILES.get(robot.id, {}).get("max_payload", 500.0)
                if request.payload_kg > max_cap:
                    continue  # Disqualified due to payload capacity
                if robot.battery < 15.0:
                    continue  # Disqualified due to critical battery

                curr_node = find_closest_node(robot.position.x, robot.position.y)
                pickup_node = request.pickup if request.pickup in WAREHOUSE_NODES else "DOCK-W"
                dist = math.hypot(
                    WAREHOUSE_NODES[pickup_node].x - robot.position.x,
                    WAREHOUSE_NODES[pickup_node].y - robot.position.y,
                )
                margin = max_cap - request.payload_kg
                bid_score = (margin * 0.05) + (robot.battery * 0.40) - (dist * 0.08) + (request.priority * 0.25)
                eligible.append((robot, bid_score))

            winner = max(eligible, key=lambda pair: pair[1])[0] if eligible else None

            task = await self._task_store.create(
                pickup=request.pickup,
                destination=request.destination,
                priority=request.priority,
                assigned_robot_id=winner.id if winner else None,
                payload_kg=request.payload_kg,
                payload_size=request.payload_size,
                urgency=request.urgency,
            )

            if winner:
                # Compute A* path from robot current position to pickup, then destination
                blocked = {"AISLE-B07"} if self._state.aisle_blocked else set()
                curr_node = find_closest_node(winner.position.x, winner.position.y)
                pickup_node = request.pickup if request.pickup in WAREHOUSE_NODES else "DOCK-W"
                dest_node = request.destination if request.destination in WAREHOUSE_NODES else "DOCK-E"

                path_pickup = find_shortest_path(curr_node, pickup_node, blocked)
                path_dest = find_shortest_path(pickup_node, dest_node, blocked)
                full_coords = path_pickup + (path_dest[1:] if path_dest else [])

                winner.path = _to_points(full_coords)
                winner.path_index = 0
                winner.progress = 0.0
                winner.task = f"{task.id}: {request.pickup} -> {request.destination} ({request.payload_kg}kg)"
                winner.status = "Moving"
                winner.priority = min(100, max(winner.priority, request.priority))
                self._state.messages += 3
                self._append_event(
                    "HANDOFF",
                    f"{winner.id} won {task.id} ({request.payload_kg}kg {request.payload_size}) via Contract-Net Protocol",
                )

            snapshot = self._state.model_copy(deep=True)
        await self._broadcast(snapshot)
        return task

    async def update_task(self, task_id: str, request: TaskUpdate) -> TaskRecord | None:
        async with self._lock:
            task = await self._task_store.update(
                task_id=task_id,
                priority=request.priority,
                payload_kg=request.payload_kg,
                payload_size=request.payload_size,
                urgency=request.urgency,
                assigned_robot_id=request.assigned_robot_id,
                status=request.status,
            )
            if task:
                self._state.messages += 2
                self._append_event("HANDOFF", f"Task {task_id} attributes reconfigured in peer mesh")
                snapshot = self._state.model_copy(deep=True)
            else:
                snapshot = None
        if snapshot is not None:
            await self._broadcast(snapshot)
        return task

    async def delete_task(self, task_id: str) -> bool:
        async with self._lock:
            success = await self._task_store.delete(task_id)
            if success:
                self._append_event("HANDOFF", f"Task {task_id} withdrawn from auction queue")
                snapshot = self._state.model_copy(deep=True)
            else:
                snapshot = None
        if snapshot is not None:
            await self._broadcast(snapshot)
        return success

    async def list_tasks(self) -> list[TaskRecord]:
        async with self._lock:
            return await self._task_store.list()

    async def complete_task(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            task = await self._task_store.complete(task_id)
            if task is not None:
                self._state.completed_tasks += 1
                self._append_event("HEARTBEAT", f"{task.id} completed by {task.assigned_robot_id or 'fleet'}")
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

        # Clear expired corridor mutex lease
        if self._state.reservation and (
            self._state.tick >= self._state.lease_until
            or self._has_cleared_conflict(self._require_robot(self._state.reservation))
        ):
            holder = self._state.reservation
            self._state.reservation = None
            self._state.lease_until = 0
            self._append_event("LEASE", f"{holder} cleared Corridor C-14 & released distributed mutex")

        # Negotiate approaching contenders
        contenders = sorted(
            (robot for robot in self._state.robots if self._is_approaching_conflict(robot)),
            key=self._priority_score,
            reverse=True,
        )
        if self._state.reservation is None and contenders:
            winner = contenders[0]
            self._state.reservation = winner.id
            self._state.lease_until = self._state.tick + 8
            self._append_event("LEASE", f"{winner.id} leased C-14 for 4.8s | priority {winner.priority}")

        # Step robots along A* path
        for robot in self._state.robots:
            if self._is_approaching_conflict(robot) and self._state.reservation != robot.id:
                robot.status = "Yielding"
            else:
                speed = ROBOT_PROFILES.get(robot.id, {}).get("speed", 20.0)
                self._move(robot, speed)

        if self._state.tick % 8 == 0:
            self._append_event("HEARTBEAT", "Peer heartbeat quorum 3/3 | state convergence 84 ms")

    def _move(self, robot: RobotState, speed: float) -> None:
        next_index = robot.path_index + 1
        if next_index >= len(robot.path):
            # Completed mission leg: recompute next A* patrol or recharge
            robot.completed += 1
            robot.battery = min(100.0, robot.battery + 1.2)
            curr_node = find_closest_node(robot.position.x, robot.position.y)
            targets = ["DOCK-E", "DOCK-W", "CHARGE", "RACK A-03", "RACK B-02"]
            next_target = targets[(self._state.tick + robot.completed) % len(targets)]
            blocked = {"AISLE-B07"} if self._state.aisle_blocked else set()
            robot.path = _to_points(find_shortest_path(curr_node, next_target, blocked))
            robot.path_index = 0
            robot.progress = 0.0
            robot.status = "Moving"
            return

        start = robot.path[robot.path_index]
        end = robot.path[next_index]
        distance = math.hypot(end.x - start.x, end.y - start.y) or 1.0

        # Obstacle avoidance LiDAR interlock
        if self._state.aisle_blocked and robot.status != "Rerouting":
            next_x = start.x + (end.x - start.x) * (robot.progress + speed / distance)
            next_y = start.y + (end.y - start.y) * (robot.progress + speed / distance)
            if math.hypot(next_x - 640.0, next_y - 415.0) < 50.0:
                # Dynamic D* Lite reroute around B-07
                curr_node = find_closest_node(robot.position.x, robot.position.y)
                robot.path = _to_points(find_shortest_path(curr_node, "DOCK-W", {"AISLE-B07"}))
                robot.path_index = 0
                robot.progress = 0.0
                robot.status = "Rerouting"
                self._append_event("REROUTE", f"{robot.id} detected obstacle; recalculated A* detour")
                return

        progress = robot.progress + speed / distance
        robot.status = "Moving"
        robot.battery = max(10.0, robot.battery - 0.05)

        if progress >= 1.0:
            robot.path_index = next_index
            robot.progress = 0.0
            robot.position = end.model_copy()
        else:
            robot.progress = progress
            robot.position = Point(
                x=start.x + (end.x - start.x) * progress,
                y=start.y + (end.y - start.y) * progress,
            )

    def _is_approaching_conflict(self, robot: RobotState) -> bool:
        if robot.path_index + 1 >= len(robot.path):
            return False
        next_point = robot.path[robot.path_index + 1]
        return math.hypot(next_point.x - CONFLICT_POINT.x, next_point.y - CONFLICT_POINT.y) < 10.0 and robot.progress > 0.45

    @staticmethod
    def _has_cleared_conflict(robot: RobotState) -> bool:
        return robot.path_index > 2

    @staticmethod
    def _priority_score(robot: RobotState) -> float:
        return robot.priority + (100.0 - robot.battery) * 0.2

    def _require_robot(self, robot_id: RobotId) -> RobotState:
        for robot in self._state.robots:
            if robot.id == robot_id:
                return robot
        raise ValueError(f"Unknown robot {robot_id}")

    def _append_event(self, event_type: FleetEvent.type, message: str) -> None:
        self._state.events.insert(0, _event(self._state.tick, event_type, message))
        del self._state.events[8:]
