"""Behavioural tests for the fleet coordinator: task lifecycle, persistence, replanning, mutex safety."""
from __future__ import annotations

import asyncio
import random

from app.coordinator import FleetCoordinator
from app.database import Database
from app.graph import ROBOT_FLEET, WAREHOUSE_NODES, RobotSpec
from app.schemas import BlockageRequest, TaskCreate, TaskUpdate


class World:
    def __init__(self, tmp_path, fleet=ROBOT_FLEET) -> None:
        self.url = f"sqlite+aiosqlite:///{(tmp_path / 'fleet.db').as_posix()}"
        self.fleet = fleet

    async def boot(self) -> FleetCoordinator:
        db = Database(self.url)
        await db.initialize_test_schema()
        coord = FleetCoordinator(db, fleet=self.fleet)
        await coord.start(ticker=False)
        return coord


async def step(coord: FleetCoordinator, ticks: int = 1):
    for _ in range(ticks):
        async with coord._lock:
            coord._step()
    await coord._flush()
    return await coord.snapshot()


def task(pickup: str, destination: str, kg: float = 100.0, priority: int = 50) -> TaskCreate:
    return TaskCreate(pickup=pickup, destination=destination, payload_kg=kg, priority=priority)


def run(coro):
    return asyncio.run(coro)


def test_task_runs_full_lifecycle_and_is_persisted(tmp_path) -> None:
    async def scenario():
        world = World(tmp_path)
        coord = await world.boot()
        created = await coord.create_task(task("RACK A-02", "DOCK-E"))
        assert created.status == "Assigned"
        seen = {created.status}
        for _ in range(400):
            state = await step(coord)
            seen.add(next(t.status for t in state.tasks if t.id == created.id))
            if "Completed" in seen:
                break
        assert seen == {"Assigned", "In Progress", "Completed"}
        assert state.completed_tasks == 1 and state.kpis.completed_total == 1
        robot = next(r for r in state.robots if r.completed == 1)
        assert robot.task_id is None and robot.current_payload_kg == 0
        # a fresh coordinator on the same database sees the persisted result
        again = await world.boot()
        assert [t.status for t in await again.list_tasks()] == ["Completed"]
        assert (await again.snapshot()).completed_tasks == 1

    run(scenario())


def test_restart_requeues_in_flight_tasks(tmp_path) -> None:
    async def scenario():
        world = World(tmp_path)
        coord = await world.boot()
        created = await coord.create_task(task("RACK A-02", "DOCK-E"))
        await step(coord, 5)
        again = await world.boot()
        state = await again.snapshot()
        record = next(t for t in state.tasks if t.id == created.id)
        assert record.status == "Assigned"  # re-auctioned after being requeued
        assert any(r.task_id == created.id for r in state.robots)

    run(scenario())


def test_capacity_and_unreachable_tasks_are_reported_honestly(tmp_path) -> None:
    async def scenario():
        coord = await World(tmp_path).boot()
        heavy = await coord.create_task(task("RACK A-01", "DOCK-E", kg=1500))
        assert heavy.status == "Blocked"  # no AMR can carry 1500kg
        mid = await coord.create_task(task("RACK A-03", "DOCK-E", kg=800))
        state = await coord.snapshot()
        assert next(r for r in state.robots if r.task_id == mid.id).id == "AMR-01"
        # RACK B-03 is only reachable through AISLE-B07
        await coord.report_blockage(BlockageRequest(aisle_id="B-07"))
        stuck = await coord.create_task(task("RACK B-03", "DOCK-E", kg=50))
        assert stuck.status in ("Queued", "Blocked")
        await coord.report_blockage(BlockageRequest(aisle_id="B-07", blocked=False))
        state = await step(coord)
        assert next(t for t in state.tasks if t.id == stuck.id).status == "Assigned"

    run(scenario())


def test_blockage_reroutes_and_mission_still_completes(tmp_path) -> None:
    async def scenario():
        coord = await World(tmp_path).boot()
        await coord.create_task(task("BYPASS-W", "BYPASS-E", kg=100))
        await step(coord, 2)
        blocked_xy = (WAREHOUSE_NODES["AISLE-B07"].x, WAREHOUSE_NODES["AISLE-B07"].y)
        state = await coord.report_blockage(BlockageRequest(aisle_id="AISLE-B07"))
        for robot in state.robots:
            assert all((p.x, p.y) != blocked_xy for p in robot.path[robot.path_index + 1:])
        for _ in range(600):
            state = await step(coord)
            if state.completed_tasks:
                break
        assert state.completed_tasks == 1 and state.collision_count == 0

    run(scenario())


def test_fleet_is_data_a_fourth_robot_needs_no_code_change(tmp_path) -> None:
    async def scenario():
        fleet = ROBOT_FLEET + (RobotSpec("AMR-04", "Zed", "#22C55E", 500.0, 22.0, "INT-N2", 99.0),)
        coord = await World(tmp_path, fleet).boot()
        assert len((await coord.snapshot()).robots) == 4
        for pickup in ("RACK A-01", "RACK A-02", "RACK A-03", "RACK A-04"):
            await coord.create_task(task(pickup, "DOCK-E", kg=50))
        state = await coord.snapshot()
        assert {r.id for r in state.robots if r.task_id} == {"AMR-01", "AMR-02", "AMR-03", "AMR-04"}

    run(scenario())


def test_head_on_traffic_through_mutex_zone_is_collision_free(tmp_path) -> None:
    async def scenario():
        coord = await World(tmp_path).boot()
        await coord.create_task(task("DOCK-W", "DOCK-E", kg=100, priority=70))
        await coord.create_task(task("DOCK-E", "DOCK-W", kg=100, priority=60))
        lease_seen = False
        for _ in range(600):
            state = await step(coord)
            lease_seen |= "C-14" in state.leases
            if state.completed_tasks == 2:
                break
        assert state.completed_tasks == 2
        assert state.collision_count == 0
        assert lease_seen, "C-14 should have been leased at least once"

    run(scenario())


def test_manual_task_management_reassigns_and_withdraws(tmp_path) -> None:
    async def scenario():
        coord = await World(tmp_path).boot()
        created = await coord.create_task(task("RACK A-02", "DOCK-E", kg=100))
        state = await coord.snapshot()
        owner = next(r for r in state.robots if r.task_id == created.id)
        other = next(r for r in state.robots if r.id != owner.id and r.id != "AMR-02")
        updated = await coord.update_task(created.id, TaskUpdate(assigned_robot_id=other.id))
        assert updated.assigned_robot_id == other.id
        state = await coord.snapshot()
        assert next(r for r in state.robots if r.id == other.id).task_id == created.id
        assert next(r for r in state.robots if r.id == owner.id).task_id is None
        assert await coord.delete_task(created.id)
        state = await step(coord)
        assert all(r.task_id is None for r in state.robots) and not state.tasks

    run(scenario())


def test_random_workload_completes_without_collisions_or_deadlock(tmp_path) -> None:
    async def scenario():
        rng = random.Random(26123)
        coord = await World(tmp_path).boot()
        nodes = list(WAREHOUSE_NODES)
        created = []
        state = None
        for tick in range(2500):
            if tick % 40 == 0 and len(created) < 30:
                pickup, destination = rng.sample(nodes, 2)
                new = await coord.create_task(task(pickup, destination, kg=rng.choice((50, 300, 650)), priority=rng.randint(10, 90)))
                created.append(new.id)
            if tick == 900:
                await coord.report_blockage(BlockageRequest(aisle_id="B-07"))
            if tick == 1400:
                await coord.report_blockage(BlockageRequest(aisle_id="B-07", blocked=False))
            state = await step(coord)
        assert state.collision_count == 0
        statuses = {t.id: t.status for t in state.tasks}
        assert len(created) == 30
        assert all(statuses[i] == "Completed" for i in created), {i: statuses[i] for i in created if statuses[i] != "Completed"}
        assert all(r.status != "Blocked" for r in state.robots)

    run(scenario())


def test_warehouse_graph_has_no_unprotected_crossings() -> None:
    """Reservations are per node, so two lanes may only cross at a shared node."""
    from app.graph import _EDGES

    def xy(node):
        return WAREHOUSE_NODES[node].x, WAREHOUSE_NODES[node].y

    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(p, a, b):
        return orient(a, b, p) == 0 and min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])

    for i, (a, b) in enumerate(_EDGES):
        for c, d in _EDGES[i + 1:]:
            if {a, b} & {c, d}:
                continue
            d1, d2, d3, d4 = orient(xy(c), xy(d), xy(a)), orient(xy(c), xy(d), xy(b)), orient(xy(a), xy(b), xy(c)), orient(xy(a), xy(b), xy(d))
            assert not (d1 * d2 < 0 and d3 * d4 < 0), f"edges {(a, b)} and {(c, d)} cross without a node"
        for node in WAREHOUSE_NODES:
            if node not in (a, b):
                assert not on_segment(xy(node), xy(a), xy(b)), f"{node} lies on edge {(a, b)}"


import pytest  # noqa: E402


@pytest.mark.parametrize("seed", [4, 10, 12, 25])  # seeds that used to deadlock (dead-end racks, C-14, crossing lanes)
def test_random_blockages_never_deadlock_or_collide(tmp_path, seed) -> None:
    async def scenario():
        rng = random.Random(seed)
        coord = await World(tmp_path).boot()
        nodes = list(WAREHOUSE_NODES)
        created, blocked = [], []
        for tick in range(5000):
            if tick % 25 == 0 and len(created) < 60:
                pickup, destination = rng.sample(nodes, 2)
                new = await coord.create_task(TaskCreate(
                    pickup=pickup, destination=destination, payload_kg=rng.choice((50, 300, 650, 1000)),
                    priority=rng.randint(1, 100), urgency=rng.choice(("low", "standard", "critical")),
                ))
                created.append(new.id)
            if tick % 400 == 100 and tick < 3500:
                blocked.append(rng.choice(nodes))
                await coord.report_blockage(BlockageRequest(aisle_id=blocked[-1]))
            if tick % 400 == 300 and blocked and tick < 3500:
                await coord.report_blockage(BlockageRequest(aisle_id=blocked.pop(), blocked=False))
            if tick == 3600:
                for node in blocked:
                    await coord.report_blockage(BlockageRequest(aisle_id=node, blocked=False))
            async with coord._lock:
                coord._step()
            if tick % 5 == 0:
                await coord._flush()
        await coord._flush()
        state = await coord.snapshot()
        assert state.collision_count == 0
        unfinished = {i: t.status for i, t in coord._tasks.items() if t.status != "Completed"}
        assert not unfinished, unfinished
        persisted = {t.id: t.status for t in await coord._task_store.list()}
        assert all(persisted[i] == coord._tasks[i].status for i in created)

    run(scenario())


def test_legacy_rows_with_free_text_locations_do_not_break_startup(tmp_path) -> None:
    async def scenario():
        world = World(tmp_path)
        db = Database(world.url)
        await db.initialize_test_schema()
        from app.task_store import TaskStore

        store = TaskStore(db.sessions, db.url)
        await store.create(pickup="P-08", destination="Dock E", priority=50, assigned_robot_id=None)  # unknown pickup
        await store.create(pickup="RACK A-01", destination="Dock E", priority=50, assigned_robot_id=None)  # alias, fixable
        coord = await world.boot()
        tasks = {t.pickup: t for t in await coord.list_tasks()}
        assert tasks["P-08"].status == "Blocked"
        assert tasks["RACK A-01"].destination == "DOCK-E" and tasks["RACK A-01"].status == "Assigned"
        # the canonical form was persisted
        again = await world.boot()
        assert {t.destination for t in await again.list_tasks() if t.pickup == "RACK A-01"} == {"DOCK-E"}

    run(scenario())
