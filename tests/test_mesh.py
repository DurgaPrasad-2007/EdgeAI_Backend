"""The decentralisation claims, enforced: robots coordinate over the mesh, survive a silent peer, keep working
without the gateway, run as independent asyncio tasks, and beat stop-and-wait by the required margin."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from app import agents as agents_mod
from app import coordinator as coordinator_mod
from app.benchmark import compare
from app.coordinator import FleetCoordinator
from app.database import Database
from app.graph import WAREHOUSE_NODES
from app.schemas import SimulationControl, TaskCreate, TaskRecord
from app.sim import FleetSim


def make_task(i: int, pickup: str, destination: str, priority: int = 60, kg: float = 100.0) -> TaskRecord:
    return TaskRecord(id=f"M{i:02d}", pickup=pickup, destination=destination, priority=priority, status="Queued", payload_kg=kg,
                      created_at=datetime.now(timezone.utc) + timedelta(milliseconds=i))


def run_random_workload(seed: int, policy: str = "decentralized", ticks: int = 5000) -> FleetSim:
    rng = random.Random(seed)
    sim = FleetSim(policy=policy)
    sim.running = True
    nodes, blocked, created = list(WAREHOUSE_NODES), [], 0
    for tick in range(ticks):
        if tick % 25 == 0 and created < 60:
            pickup, destination = rng.sample(nodes, 2)
            sim.add_task(TaskRecord(id=f"R{created:02d}", pickup=pickup, destination=destination, priority=rng.randint(1, 100), status="Queued",
                                    payload_kg=rng.choice((50, 300, 650, 1000)), urgency=rng.choice(("low", "standard", "critical")),
                                    created_at=datetime.now(timezone.utc) + timedelta(seconds=created)))
            created += 1
            sim.dispatch()
        if tick % 400 == 100 and tick < 3500:
            blocked.append(rng.choice(nodes))
            sim.report_blockage(blocked[-1], True)
        if tick % 400 == 300 and blocked and tick < 3500:
            sim.report_blockage(blocked.pop(), False)
        if tick == 3600:
            for node in blocked:
                sim.report_blockage(node, False)
        sim.step()
    return sim


@pytest.mark.parametrize("seed", [0, 2, 27, 32, 34, 26123])  # seeds that exposed real bugs while the mesh was built
def test_mesh_is_collision_free_and_never_deadlocks(seed: int) -> None:
    sim = run_random_workload(seed)
    assert sim.world.collisions == 0
    assert {t.id: t.status for t in sim.tasks.values() if t.status != "Completed"} == {}


def test_stop_and_wait_baseline_is_also_safe() -> None:
    sim = run_random_workload(1, policy="stop_and_wait", ticks=3000)
    assert sim.world.collisions == 0


def test_robots_only_know_peers_through_messages() -> None:
    sim = FleetSim()
    a, b = sim.agents[0], sim.agents[1]
    assert set(a.peers) == {"AMR-02", "AMR-03"}            # learned from POSE broadcasts, nothing shared
    a.peers.clear()
    sim.bus.silenced.add(b.id)                                # b's radio goes down: a can no longer hear it
    sim.running = True
    for _ in range(3):
        sim.step()
    assert "AMR-02" not in a.peers


def test_silent_robot_claims_expire_and_its_task_is_reassigned() -> None:
    sim = FleetSim()
    sim.running = True
    sim.add_task(make_task(1, "RACK A-03", "DOCK-W"))
    sim.dispatch()
    victim = next(a for a in sim.agents if a.task)
    for _ in range(10):
        sim.step()
    victim.go_silent(ticks=400)
    for _ in range(300):
        sim.step()
    task = sim.tasks["M01"]
    assert task.status == "Completed" and task.assigned_robot_id != victim.id  # a surviving peer finished it
    assert sim.world.collisions == 0
    survivors = [a for a in sim.agents if a is not victim]
    assert all(not a._alive(a.peers[victim.id]) for a in survivors)              # heartbeat timeout: its claims lapsed
    assert any("lost heartbeat" in e.message for e in sim.events)


def test_mesh_keeps_moving_and_negotiating_without_the_gateway() -> None:
    sim = FleetSim()
    sim.running = True
    sim.add_task(make_task(1, "DOCK-W", "DOCK-E", priority=90))
    sim.add_task(make_task(2, "DOCK-E", "DOCK-W", priority=60))
    sim.dispatch()
    assert sum(1 for a in sim.agents if a.task) == 2
    sim.dispatch = lambda: None  # the task board / gateway goes away: robots already holding work carry on alone
    for _ in range(600):
        sim.step()
        if all(t.status == "Completed" for t in sim.tasks.values()):
            break
    assert all(t.status == "Completed" for t in sim.tasks.values())
    assert sim.world.collisions == 0


def test_decentralised_beats_stop_and_wait_by_at_least_20_percent() -> None:
    result = compare()
    assert result["all_completed"]
    assert result["collisions_decentralized"] == 0 and result["collisions_stop_and_wait"] == 0
    assert result["reduction_pct"] >= result["target_pct"], result


def test_live_mode_runs_every_robot_as_its_own_asyncio_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agents_mod, "CONTROL_PERIOD_SECONDS", 0.01)
    monkeypatch.setattr(coordinator_mod, "CONTROL_PERIOD_SECONDS", 0.01)

    async def scenario():
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'live.db').as_posix()}")
        await db.initialize_test_schema()
        coord = FleetCoordinator(db)
        await coord.start(ticker=True)
        try:
            assert len(coord._agent_tasks) == 2 * len(coord._sim.agents)  # a control loop and a mesh listener per robot
            await coord.set_simulation(SimulationControl(running=True))
            first = await coord.create_task(TaskCreate(pickup="DOCK-W", destination="DOCK-E", payload_kg=100, priority=80))
            second = await coord.create_task(TaskCreate(pickup="DOCK-E", destination="DOCK-W", payload_kg=100, priority=60))
            assert first.assigned_robot_id != second.assigned_robot_id
            for _ in range(600):
                await asyncio.sleep(0.02)
                state = await coord.snapshot()
                if state.completed_tasks == 2:
                    break
            assert state.completed_tasks == 2 and state.collision_count == 0
        finally:
            await coord.stop()

    asyncio.run(scenario())


def test_commands_do_not_wait_for_a_slow_database(tmp_path, monkeypatch) -> None:
    """The real deployment talks to a remote Postgres (~1.6 s per round trip). API commands must answer from memory and
    persist behind the request, otherwise every scenario switch stalls for many seconds."""
    from app.task_store import TaskStore

    real_commit = TaskStore.commit

    async def slow_commit(self, audit_rows, ops):
        await asyncio.sleep(1.0)
        await real_commit(self, audit_rows, ops)

    async def scenario():
        import time

        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'slow.db').as_posix()}")
        await db.initialize_test_schema()
        coord = FleetCoordinator(db)
        await coord.start(ticker=False)
        monkeypatch.setattr(TaskStore, "commit", slow_commit)
        started = time.perf_counter()
        await coord.reset()
        await coord.set_simulation(SimulationControl(running=True))
        first = await coord.create_task(TaskCreate(pickup="DOCK-W", destination="DOCK-E", payload_kg=100, priority=80))
        second = await coord.create_task(TaskCreate(pickup="DOCK-E", destination="DOCK-W", payload_kg=100, priority=60))
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"commands blocked on the database for {elapsed:.2f}s"
        assert first.status == "Assigned" and second.status == "Assigned"
        await coord._flush()  # write-behind catches up
        assert {t.id for t in await coord._task_store.list()} == {first.id, second.id}
        assert {t.status for t in await coord._task_store.list()} == {"Assigned"}

    asyncio.run(scenario())


def test_snapshot_sequence_never_goes_backwards_across_reset(tmp_path) -> None:
    """Dashboards discard any state whose seq is lower than the last one they saw, so Reset must not restart the counter."""

    async def scenario():
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'seq.db').as_posix()}")
        await db.initialize_test_schema()
        coord = FleetCoordinator(db)
        await coord.start(ticker=False)
        await coord.set_simulation(SimulationControl(running=True))
        for _ in range(50):
            async with coord._lock:
                coord._step()
                coord._sim.snapshot()
        before = (await coord.snapshot()).seq
        after_reset = (await coord.reset()).seq
        after_next = (await coord.set_simulation(SimulationControl(running=True))).seq
        assert before < after_reset < after_next

    asyncio.run(scenario())


def test_reset_can_drop_unfinished_jobs_for_a_clean_scenario(tmp_path) -> None:
    async def scenario():
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'clear.db').as_posix()}")
        await db.initialize_test_schema()
        coord = FleetCoordinator(db)
        await coord.start(ticker=False)
        await coord.set_simulation(SimulationControl(running=True))
        for pickup, drop in (("DOCK-W", "DOCK-E"), ("DOCK-E", "DOCK-W"), ("RACK A-02", "RACK C-02"), ("RACK B-01", "RACK A-04")):
            await coord.create_task(TaskCreate(pickup=pickup, destination=drop, payload_kg=100, priority=70))
        await coord._flush()
        requeued = await coord.reset()  # default: in-flight jobs go back on the queue and are re-auctioned
        assert len(requeued.tasks) == 4 and any(r.task_id for r in requeued.robots)
        cleared = await coord.reset(clear_tasks=True)
        assert cleared.tasks == [] and all(r.task_id is None and r.status == "Idle" for r in cleared.robots)
        await coord._flush()
        assert await coord._task_store.list() == []  # gone from the database too

    asyncio.run(scenario())
