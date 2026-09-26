"""Measured comparison: decentralised mesh vs classic stop-and-wait on overlapping paths.

Both policies run the *same* robots, physics, safety reservations and workload (headless, deterministic).
They differ only in how conflicts are handled:

* stop-and-wait: a robot that meets a conflict stops until the path clears; it only tries a detour or
  side-step after a long timeout; it plans without regard for where peers are heading.
* decentralised: robots advertise intent, the closer/higher-utility robot gets right of way at the choke
  point, robots route around congestion, and mutual waits are broken locally and immediately.
"""
from __future__ import annotations

import random
import statistics
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from app.graph import ROBOT_FLEET
from app.schemas import TaskRecord
from app.agents import Msg
from app.sim import FleetSim

WEST = ["DOCK-W", "RACK A-01", "RACK A-02", "RACK B-01", "RACK B-02", "INT-W1", "BYPASS-W"]
EAST = ["DOCK-E", "RACK A-03", "RACK A-04", "RACK B-03", "RACK B-04", "INT-E2", "BYPASS-E"]
NORTH_SOUTH = [("INT-N1", "INT-S2"), ("INT-S2", "INT-N1"), ("RACK A-02", "RACK C-02"), ("RACK C-03", "RACK A-03")]
MISSIONS_PER_ROBOT = 4
TICK_CAP = 4000
SEEDS = tuple(range(1, 101))  # fixed in advance; never tuned to the result


def workload(seed: int) -> dict[str, list[tuple[str, str, int, float]]]:
    """A fixed mission queue per robot. Every robot's missions cross the C-14 choke point or the spine, so the
    fleet's routes overlap constantly. Both policies get exactly the same assignments: only conflict handling differs."""
    rng = random.Random(seed)
    queues: dict[str, list[tuple[str, str, int, float]]] = {}
    for n, spec in enumerate(ROBOT_FLEET):
        jobs = []
        for i in range(MISSIONS_PER_ROBOT):
            west_first = (n + i) % 2 == 0
            if (n + i) % 3 == 2:
                pickup, drop = rng.choice(NORTH_SOUTH)
            else:
                pickup, drop = (rng.choice(WEST), rng.choice(EAST)) if west_first else (rng.choice(EAST), rng.choice(WEST))
            jobs.append((pickup, drop, rng.randint(30, 95), rng.choice((60.0, 150.0, 300.0))))
        queues[spec.id] = jobs
    return queues


def run_once(policy: str, seed: int) -> dict:
    sim = FleetSim(ROBOT_FLEET, policy=policy)
    sim.running = True
    now = datetime.now(timezone.utc)
    queues = {rid: list(jobs) for rid, jobs in workload(seed).items()}
    total = sum(len(q) for q in queues.values())
    n = 0
    for rid, jobs in queues.items():
        for pickup, drop, priority, kg in jobs:
            sim.add_task(TaskRecord(id=f"B{n:02d}", pickup=pickup, destination=drop, priority=priority, status="Queued", payload_kg=kg, created_at=now + timedelta(milliseconds=n)))
            n += 1
    order = {rid: [f"B{i:02d}" for i in range(k * MISSIONS_PER_ROBOT, (k + 1) * MISSIONS_PER_ROBOT)] for k, rid in enumerate(queues)}
    finished_at = None
    started: dict[str, int] = {}
    ended: dict[str, int] = {}
    waiting = 0
    for _ in range(TICK_CAP):
        for agent in sim.agents:  # the work order goes to a specific robot (a directive); everything after that is the mesh's job
            if order[agent.id] and agent.is_free():
                task = sim.tasks[order[agent.id][0]]
                sim.bus.publish(Msg("TASK_DIRECTIVE", "WMS", agent.id, {"robot": agent.id, "task": sim._payload(task)}, sim.clock.tick))
                sim.bus.pump()
                if agent.task and agent.task["id"] == task.id:
                    order[agent.id].pop(0)
        sim.step()
        waiting += sum(1 for a in sim.agents if a.status == "Yielding")
        for t in sim.tasks.values():
            if t.status != "Queued":
                started.setdefault(t.id, sim.clock.tick)
            if t.status == "Completed":
                ended.setdefault(t.id, sim.clock.tick)
        if sum(1 for t in sim.tasks.values() if t.status == "Completed") == total:
            finished_at = sim.clock.tick
            break
    done = sum(1 for t in sim.tasks.values() if t.status == "Completed")
    mission_ticks = sum(ended.get(i, TICK_CAP) - started.get(i, 0) for i in sim.tasks)
    return {"ticks": finished_at or TICK_CAP, "mission_ticks": mission_ticks, "waiting_ticks": waiting, "finished": finished_at is not None, "completed": done, "collisions": sim.world.collisions}


def _pct(base: float, mesh: float) -> float:
    return round(100 * (base - mesh) / base, 1) if base else 0.0


@lru_cache(maxsize=1)
def compare() -> dict:
    """Run every seed under both policies. Cached: the workload is fixed, so the answer is too."""
    rows = []
    for seed in SEEDS:
        base, mesh = run_once("stop_and_wait", seed), run_once("decentralized", seed)
        rows.append({"seed": seed, "stop_and_wait": base, "decentralized": mesh, "reduction_pct": _pct(base["mission_ticks"], mesh["mission_ticks"])})
    total = lambda policy, key: sum(r[policy][key] for r in rows)  # noqa: E731
    period, n = 0.6, len(rows)
    return {
        "workload": f"{MISSIONS_PER_ROBOT * len(ROBOT_FLEET)} overlapping missions (identical assignments for both policies), {n} randomised runs, {len(ROBOT_FLEET)} AMRs",
        # headline: the success criterion, "total task completion time", = sum of every mission's completion time
        "reduction_pct": _pct(total("stop_and_wait", "mission_ticks"), total("decentralized", "mission_ticks")),
        "stop_and_wait_mission_seconds": round(total("stop_and_wait", "mission_ticks") / n * period, 1),
        "decentralized_mission_seconds": round(total("decentralized", "mission_ticks") / n * period, 1),
        "makespan_reduction_pct": _pct(total("stop_and_wait", "ticks"), total("decentralized", "ticks")),
        "stop_and_wait_makespan_seconds": round(total("stop_and_wait", "ticks") / n * period, 1),
        "decentralized_makespan_seconds": round(total("decentralized", "ticks") / n * period, 1),
        "waiting_reduction_pct": _pct(total("stop_and_wait", "waiting_ticks"), total("decentralized", "waiting_ticks")),
        "median_reduction_pct": round(statistics.median(r["reduction_pct"] for r in rows), 1),
        "runs_slower": sum(1 for r in rows if r["reduction_pct"] < 0),
        "runs": n,
        "collisions_stop_and_wait": total("stop_and_wait", "collisions"),
        "collisions_decentralized": total("decentralized", "collisions"),
        "all_completed": all(r["stop_and_wait"]["finished"] and r["decentralized"]["finished"] for r in rows),
        "target_pct": 20,
        "detail": rows,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(compare(), indent=2))
