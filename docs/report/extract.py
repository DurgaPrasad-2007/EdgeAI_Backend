"""Pull every number/trace used in the report straight from the repository code (run with the backend venv)."""
import datetime, json, random, statistics, sys, time
sys.path.insert(0, r"E:\Edge AI\EdgeAI_Backend")
from app.agents import Msg, CONTROL_PERIOD_SECONDS
from app.benchmark import compare
from app.graph import ROBOT_FLEET, WAREHOUSE_NODES, GRAPH_ADJACENCY, MUTEX_ZONES, RobotSpec, find_shortest_node_path, node_kind, _EDGES
from app.schemas import TaskRecord
from app.sim import FleetSim

NOW = datetime.datetime.now(datetime.timezone.utc)
def task(i, p, d, pri=60, kg=100.0):
    return TaskRecord(id=f"T{i}", pickup=p, destination=d, priority=pri, status="Queued", payload_kg=kg, created_at=NOW + datetime.timedelta(milliseconds=i))

out = {}
out["graph"] = {"nodes": [{"id": n.id, "x": n.x, "y": n.y, "label": n.label, "kind": node_kind(n.id)} for n in WAREHOUSE_NODES.values()],
                "edges": [list(e) for e in _EDGES], "mutex": sorted(MUTEX_ZONES),
                "robots": [{"id": r.id, "name": r.name, "home": r.home, "kg": r.max_payload, "speed": r.speed, "battery": r.battery} for r in ROBOT_FLEET]}

# ---- benchmark (per seed)
c = compare()
out["benchmark"] = {k: v for k, v in c.items() if k != "detail"}
out["benchmark"]["detail"] = [{"seed": r["seed"], "sw_mission": r["stop_and_wait"]["mission_ticks"], "dec_mission": r["decentralized"]["mission_ticks"],
                               "sw_make": r["stop_and_wait"]["ticks"], "dec_make": r["decentralized"]["ticks"],
                               "sw_wait": r["stop_and_wait"]["waiting_ticks"], "dec_wait": r["decentralized"]["waiting_ticks"], "red": r["reduction_pct"]} for r in c["detail"]]

# ---- head-on trace at C-14 (both policies)
def head_on(policy):
    sim = FleetSim(policy=policy); sim.running = True
    sim.add_task(task(0, "DOCK-W", "DOCK-E", 95, 250)); sim.add_task(task(1, "DOCK-E", "DOCK-W", 65, 180)); sim.dispatch()
    owners = {t.id: t.assigned_robot_id for t in sim.tasks.values()}
    rows = []
    for _ in range(600):
        sim.step()
        rows.append({"t": sim.clock.tick, "a": {a.id: {"x": a.pos.x, "y": a.pos.y, "s": a.status, "c14": "C-14" in a.claims} for a in sim.agents}})
        if all(t.status == "Completed" for t in sim.tasks.values()): break
    return {"owners": owners, "rows": rows, "done": sim.clock.tick, "collisions": sim.world.collisions, "ranks": {a.id: a._rank()[0] for a in sim.agents}}
out["head_on"] = {"decentralized": head_on("decentralized"), "stop_and_wait": head_on("stop_and_wait")}

# ---- auction message flow
sim = FleetSim(); sim.running = True
msgs = []
sim.bus.taps.append(lambda m: msgs.append({"type": m.type, "from": m.sender, "to": m.to, "tick": m.tick, "body": {k: v for k, v in m.body.items() if k in ("task", "score", "task_id", "robot", "status")}}) if m.type not in ("POSE",) else None)
sim.add_task(task(0, "RACK A-02", "DOCK-E", 90, 320)); sim.dispatch()
out["auction"] = {"msgs": msgs, "robots": [{"id": a.id, "kg": a.spec.max_payload, "bat": a.battery, "home": a.spec.home} for a in sim.agents]}

# ---- dead-end swap deadlock (default fleet, normal operation)
sim = FleetSim(); sim.running = True
ev = []
sim.bus.event_sinks.append(lambda t, m: ev.append({"t": sim.clock.tick, "type": t, "msg": m}))
sim.add_task(task(0, "RACK A-02", "RACK B-02", 80, 100)); sim.add_task(task(1, "RACK B-02", "RACK A-02", 70, 100)); sim.dispatch()
rows = []
for _ in range(400):
    sim.step()
    rows.append({"t": sim.clock.tick, "a": {a.id: {"n": a.closest_node(), "x": a.pos.x, "y": a.pos.y, "s": a.status, "stuck": a.stuck, "wf": a.wait_for} for a in sim.agents}})
    if all(t.status == "Completed" for t in sim.tasks.values()): break
out["deadlock"] = {"rows": rows, "events": ev, "owners": {t.id: t.assigned_robot_id for t in sim.tasks.values()}, "done": sim.clock.tick, "collisions": sim.world.collisions}

# ---- dropout
sim = FleetSim(); sim.running = True
ev, msgs = [], []
sim.bus.event_sinks.append(lambda t, m: ev.append({"t": sim.clock.tick, "type": t, "msg": m}))
sim.bus.taps.append(lambda m: msgs.append({"type": m.type, "from": m.sender, "tick": m.tick, "body": {k: v for k, v in m.body.items() if k in ("task_id", "robot", "status", "score")}}) if m.type in ("ORPHAN", "TASK_AWARD", "TASK_STATUS", "AGENT_FAULT") else None)
for i, (p, d) in enumerate([("RACK A-03", "DOCK-W"), ("DOCK-E", "DOCK-W")]): sim.add_task(task(i, p, d, 80 - 10 * i, 100))
sim.dispatch()
victim = next(a for a in sim.agents if a.task and a.task["id"] == "T0")
first_owner = victim.id
rows = []
for _ in range(500):
    if sim.clock.tick == 8: victim.go_silent(30)
    sim.step()
    survivors = [a for a in sim.agents if a is not victim]
    rows.append({"t": sim.clock.tick, "victim_online": victim.online, "seen_alive": [bool(a.peers.get(victim.id) and a._alive(a.peers[victim.id])) for a in survivors], "owner_t0": sim.tasks["T0"].assigned_robot_id, "st_t0": sim.tasks["T0"].status})
    if all(t.status == "Completed" for t in sim.tasks.values()): break
out["dropout"] = {"victim": first_owner, "rows": rows, "events": ev, "msgs": msgs, "done": sim.clock.tick, "collisions": sim.world.collisions, "final_owner_t0": sim.tasks["T0"].assigned_robot_id}

# ---- scaling
EXTRA = ["INT-N2", "INT-S2", "DETOUR-SW", "BYPASS-E", "RACK C-02", "INT-W1", "RACK A-04"]
def mk(n):
    f = list(ROBOT_FLEET)
    for i in range(n - len(f)): f.append(RobotSpec(f"AMR-{len(f)+1:02d}", f"X{i}", "#888", 700.0, 21.0, EXTRA[i], 95.0))
    return tuple(f[:n])
def scale(n, ticks):
    rng = random.Random(7); sim = FleetSim(mk(n)); sim.running = True
    cnt = {"yield": 0, "detour": 0, "sidestep": 0}
    sim.bus.event_sinks.append(lambda t, m: [cnt.__setitem__(k, cnt[k] + 1) for k, s in (("yield", "yielding"), ("detour", "detoured"), ("sidestep", "stepped aside")) if s in m])
    nodes = list(WAREHOUSE_NODES); made = 0; per = []
    for tick in range(ticks):
        if tick % 20 == 0 and made < 15 * n:
            p, d = rng.sample(nodes, 2); sim.add_task(task(made, p, d, rng.randint(1, 100), rng.choice((50, 200, 300)))); made += 1; sim.dispatch()
        t0 = time.perf_counter(); sim.step(); per.append((time.perf_counter() - t0) * 1000)
    per.sort()
    return {"robots": n, "ticks": ticks, "tasks": made, "done": sum(1 for t in sim.tasks.values() if t.status == "Completed"), "collisions": sim.world.collisions,
            "msgs_per_tick": round(sim.messages / ticks, 1), "tick_ms_mean": round(statistics.mean(per), 3), "tick_ms_p95": round(per[int(len(per) * .95)], 3), **cnt}
out["scaling"] = [scale(3, 2500), scale(6, 2500), scale(10, 2500), scale(10, 6000)]

# ---- planner latency
rng = random.Random(1); nodes = list(WAREHOUSE_NODES); lat = []
for _ in range(20000):
    a, b = rng.sample(nodes, 2); t0 = time.perf_counter(); find_shortest_node_path(a, b, set()); lat.append((time.perf_counter() - t0) * 1e6)
lat.sort()
out["astar_us"] = {"n": len(lat), "mean": round(statistics.mean(lat), 1), "p50": round(lat[len(lat) // 2], 1), "p95": round(lat[int(len(lat) * .95)], 1), "max": round(lat[-1], 0), "nodes": len(nodes)}
json.dump(out, open("data.json", "w"))
print("ok", {k: (len(v) if hasattr(v, "__len__") else v) for k, v in out.items()})
print(out["head_on"]["decentralized"]["done"], out["head_on"]["stop_and_wait"]["done"], out["dropout"]["victim"], out["dropout"]["final_owner_t0"], out["deadlock"]["done"], out["deadlock"]["collisions"])
