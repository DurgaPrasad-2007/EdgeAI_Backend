"""The mesh runtime: robot agents + peer bus + physics, plus the passive observer the dashboard reads.

Nothing here steers a robot. `FleetSim` wires agents to the bus, feeds them the work order stream (announcing
tasks to the mesh), and *listens* to what the robots say (awards, progress, obstacle alerts) so the task
board and the dashboard stay in sync. Robots decide everything else themselves.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from app.agents import (
    CONTROL_PERIOD_SECONDS,
    HEARTBEAT_EVERY,
    LEASE_TICKS,
    REANNOUNCE_TICKS,
    Clock,
    Msg,
    PeerBus,
    RobotAgent,
    World,
    label,
)
from app.graph import MUTEX_ZONES, ROBOT_FLEET, WAREHOUSE_NODES, RobotSpec, find_shortest_node_path
from app.schemas import FleetEvent, FleetState, Kpis, P2PMessage, TaskRecord

MAX_EVENTS = 30
MAX_P2P = 60
RECENT_COMPLETED = 50
URGENCY_BONUS = {"low": 0, "standard": 5, "critical": 25}


def _aware(value):
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FleetSim:
    def __init__(
        self,
        fleet: tuple[RobotSpec, ...] = ROBOT_FLEET,
        policy: str = "decentralized",
        on_task: Callable[[TaskRecord, dict], None] | None = None,
        on_event: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.fleet, self.policy = fleet, policy
        self.tasks: dict[str, TaskRecord] = {}
        self.on_task, self.on_event = on_task, on_event
        self.running = False
        self.agents: list[RobotAgent] = []
        # Snapshot sequence numbers must only ever go up: dashboards drop any state older than the last one they saw,
        # so restarting the counter on Reset would freeze every open dashboard until it climbed back past its old value.
        self.seq = 0
        self._build(running=False)

    # ── wiring ───────────────────────────────────────────────────────────

    def _build(self, running: bool) -> None:
        self.clock = Clock()
        self.bus = PeerBus(self.clock)
        self.world = World(self.fleet)
        self.agents = [RobotAgent(spec, self.bus, self.world, self.clock, self.policy) for spec in self.fleet]
        self.bus.taps.append(self._tap)
        self.bus.event_sinks.append(self.event)
        self.running = running
        self.events: list[FleetEvent] = []
        self.p2p: list[P2PMessage] = []
        self.messages = 0
        self._event_seq = 0
        self._p2p_seq = 0
        self.session_completed = 0
        self.completed_tasks = sum(1 for t in self.tasks.values() if t.status == "Completed")
        self.announced: dict[str, tuple[int, frozenset[str]]] = {}
        self.event("HEARTBEAT", f"Peer mesh online | {len(self.agents)} autonomous AMRs, no central dispatcher ({self.policy.replace('_', '-')})")
        for agent in self.agents:  # every robot announces itself so peers know who is on the mesh
            agent.publish_pose()
        self.bus.pump()

    def agent(self, robot_id: str) -> RobotAgent | None:
        return next((a for a in self.agents if a.id == robot_id), None)

    # ── observer: events, packets and task-board bookkeeping ─────────────

    def _stamp(self) -> str:
        return f"T+{self.clock.tick * CONTROL_PERIOD_SECONDS:04.1f}s"

    def event(self, type_: str, message: str) -> None:
        self._event_seq += 1
        stamp = self._stamp()
        self.events.insert(0, FleetEvent(id=self._event_seq, time=stamp, type=type_, message=message))
        del self.events[MAX_EVENTS:]
        if self.on_event:
            self.on_event(type_, message, stamp)

    def _p2p(self, msg: Msg, type_: str, payload: str) -> None:
        self._p2p_seq += 1
        self.p2p.insert(0, P2PMessage(id=f"p2p-{self._p2p_seq}", sender=msg.sender, recipient=msg.to, type=type_, payload=payload, timestamp=self._stamp()))
        del self.p2p[MAX_P2P:]

    def _set_task(self, task: TaskRecord, **fields: Any) -> None:
        for key, value in fields.items():
            if key == "unassign":
                task.assigned_robot_id = None
            else:
                setattr(task, key, value)
        if self.on_task:
            self.on_task(task, fields)

    def _requeue(self, task: TaskRecord, why: str) -> None:
        if task.status in ("Assigned", "In Progress"):
            self._set_task(task, status="Queued", unassign=True)
            self.announced.pop(task.id, None)
            self.event("HANDOFF", f"{task.id} returned to the auction: {why}")

    def _tap(self, msg: Msg) -> None:
        self.messages += 1
        b, t = msg.body, msg.type
        task = self.tasks.get(b.get("task_id", "")) if isinstance(b, dict) else None
        if t == "POSE":
            if msg.tick % HEARTBEAT_EVERY == 0 and msg.sender in self.world.pos:
                node = self.agent(msg.sender).closest_node() if self.agent(msg.sender) else "?"
                self._p2p(msg, "HEARTBEAT", f"PEER_SYNC[battery={b['battery']:.0f}%, node={node}]")
        elif t == "MUTEX_REQ":
            self._p2p(msg, "MUTEX_REQ", f"INTENT[{b['zone']}, ETA={b['eta']:.1f}s]" if b.get("intent") else f"MUTEX_REQ[{b['zone']}, pri={b['pri']}]")
        elif t == "MUTEX_GRANT":
            self._p2p(msg, "MUTEX_GRANT", f"LEASE_ACQUIRED[{b['zone']}, pri={b['pri']}, {b['ttl'] * CONTROL_PERIOD_SECONDS:.1f}s]")
        elif t == "MUTEX_RELEASE":
            self._p2p(msg, "MUTEX_GRANT", f"RELEASE_MUTEX[{b['zone']}]")
        elif t == "YIELD_ACK":
            self._p2p(msg, "YIELD_ACK", f"YIELD[{b.get('reason') or b.get('zone')}]")
        elif t == "OBSTACLE_ALERT":
            self._p2p(msg, "OBSTACLE_ALERT", f"OBSTACLE_{'DETECTED' if b['blocked'] else 'CLEARED'}[{label(b['node'])}{' impassable' if b['blocked'] else ''}]")
        elif t == "AGENT_FAULT":
            self._p2p(msg, "OBSTACLE_ALERT", f"AGENT_FAULT[{b['fault']} {b['note']}]")
        elif t == "HEARTBEAT":
            self._p2p(msg, "HEARTBEAT", b["alert"])
        elif t == "TASK_ANNOUNCE":
            self._p2p(msg, "TASK_BID", f"TASK_ANNOUNCE[{b['task']['id']}, {b['task']['payload_kg']:g}kg, pri={b['task']['priority']}]")
        elif t == "TASK_BID":
            self._p2p(msg, "TASK_BID", f"BID[{b['task_id']}, score={b['score']:.1f}]" if b["score"] is not None else f"NO_BID[{b['task_id']}]")
        elif t == "TASK_AWARD" and task:
            self._p2p(msg, "TASK_BID", f"AUCTION_WIN[{task.id}, payload={task.payload_kg:g}kg ({task.payload_size}), pri={task.priority}, score={b['score']:.1f}]")
            if task.status in ("Queued", "Blocked", "Assigned"):
                self._set_task(task, status="Assigned", assigned_robot_id=msg.sender)
                how = "manual override" if b.get("directive") else f"decentralised Contract-Net auction (score {b['score']:.1f})"
                self.event("HANDOFF", f"{msg.sender} won {task.id} ({task.payload_kg:g}kg {task.payload_size}) via {how}")
        elif t == "TASK_STATUS" and task:
            status = b["status"]
            if status == "In Progress" and task.status == "Assigned":
                self._set_task(task, status="In Progress")
            elif status == "Completed" and task.status != "Completed":
                self._set_task(task, status="Completed")
                self.completed_tasks += 1
                self.session_completed += 1
            elif status == "Released" and task.assigned_robot_id == b["robot"]:
                self._requeue(task, f"{b['robot']} rejoined the mesh without it")
        elif t == "ORPHAN" and task and task.assigned_robot_id == b["robot"]:
            self._requeue(task, f"{b['robot']} went silent; a surviving peer handed its task back")

    # ── task board: announce work to the mesh ────────────────────────────

    def add_task(self, task: TaskRecord) -> None:
        self.tasks[task.id] = task

    def _check(self, task: TaskRecord) -> str | None:
        if task.pickup not in WAREHOUSE_NODES or task.destination not in WAREHOUSE_NODES:
            return "pickup or destination is not a location in the warehouse graph"
        if not any(task.payload_kg <= spec.max_payload for spec in self.fleet):
            return f"no AMR can carry {task.payload_kg:g}kg"
        if find_shortest_node_path(task.pickup, task.destination, set(self.world.obstacles)) is None:
            return "no route between pickup and destination"
        return None

    def _payload(self, task: TaskRecord) -> dict[str, Any]:
        return {"id": task.id, "pickup": task.pickup, "destination": task.destination, "priority": task.priority,
                "payload_kg": task.payload_kg, "payload_size": task.payload_size, "urgency": task.urgency}

    def announce(self, task: TaskRecord, only: list[str] | None = None) -> None:
        self.announced[task.id] = (self.clock.tick, frozenset(a.id for a in self.agents if a.is_free()))
        self.bus.publish(Msg("TASK_ANNOUNCE", "WMS", "MESH", {"task": self._payload(task), "only": only}, self.clock.tick))
        self.bus.pump()

    def dispatch(self) -> None:
        """Announce queued work to the mesh (highest priority first); the robots run the auction themselves."""
        pending = sorted(
            (t for t in self.tasks.values() if t.status in ("Queued", "Blocked")),
            key=lambda t: (-(t.priority + URGENCY_BONUS[t.urgency]), _aware(t.created_at)),
        )
        for task in pending:
            reason = self._check(task)
            if reason:
                if task.status != "Blocked":
                    self._set_task(task, status="Blocked")
                    self.event("HANDOFF", f"{task.id} blocked: {reason}")
                continue
            if task.status == "Blocked":
                self._set_task(task, status="Queued")
            free = frozenset(a.id for a in self.agents if a.is_free())
            if not free:
                continue
            tick, seen = self.announced.get(task.id, (-10_000, frozenset()))
            if free != seen or self.clock.tick - tick >= REANNOUNCE_TICKS:
                self.announce(task)

    # ── stepping (manual clock: tests, benchmark, headless) ──────────────

    def step(self) -> None:
        self.clock.tick += 1
        self.dispatch()
        n = len(self.agents)
        for i in range(n):  # any order is safe: robots only ever coordinate through the mesh
            self.agents[(i + self.clock.tick) % n].step()
        self.bus.pump()
        self.world.check_collisions(self.event)

    # ── operator actions (sensor / fault injection, work orders) ─────────

    def report_blockage(self, node: str, blocked: bool) -> None:
        if blocked == (node in self.world.obstacles):
            return
        (self.world.obstacles.add if blocked else self.world.obstacles.discard)(node)
        target = WAREHOUSE_NODES[node]
        reporter = self.agent(self.world.nearest(target.x, target.y))
        online = [a for a in self.agents if a.online]
        reporter = reporter if reporter and reporter.online else (online[0] if online else None)
        if reporter is None:
            return
        self.event("REROUTE", f"Obstacle {'reported' if blocked else 'cleared'} at {label(node)} · {'replanning affected routes' if blocked else 'restoring optimal routes'}")
        reporter.detect_obstacle(node, blocked)
        self.bus.pump()

    def reset(self) -> None:
        for task in self.tasks.values():
            if task.status in ("Assigned", "In Progress"):
                self._set_task(task, status="Queued", unassign=True)
        self._build(self.running)
        self.dispatch()

    # ── observer snapshot for the dashboard ──────────────────────────────

    def snapshot(self, bump: bool = True) -> FleetState:
        if bump:
            self.seq += 1
        leases: dict[str, str] = {}
        deadline: dict[str, int] = {}
        for agent in sorted(self.agents, key=lambda a: a._rank(), reverse=True):
            for zone in agent.zone_claims():
                if zone not in leases:
                    leases[zone] = agent.id
                    deadline[zone] = agent.manual_leases.get(zone, self.clock.tick + LEASE_TICKS)
        zone = min(MUTEX_ZONES) if MUTEX_ZONES else None
        holder = leases.get(zone) if zone else None
        ordered = sorted(self.tasks.values(), key=lambda t: _aware(t.created_at), reverse=True)
        done = [t for t in ordered if t.status == "Completed"][:RECENT_COMPLETED]
        robots = [a.telemetry() for a in self.agents]
        n = max(1, len(robots))
        hours = self.clock.tick * CONTROL_PERIOD_SECONDS / 3600
        blocked = sorted(self.world.obstacles)
        return FleetState(
            seq=self.seq, tick=self.clock.tick, running=self.running, aisle_blocked=bool(blocked), blocked_nodes=blocked,
            reservation=holder, lease_until=deadline.get(zone, 0) if holder else 0, leases=leases,
            completed_tasks=self.completed_tasks, collision_count=self.world.collisions, messages=self.messages,
            events=[e.model_copy() for e in self.events], p2p=[m.model_copy() for m in self.p2p], robots=robots,
            tasks=[t.model_copy() for t in ordered if t.status != "Completed"] + [t.model_copy() for t in done],
            mode=self.policy,
            kpis=Kpis(
                fleet_utilization_pct=round(100 * sum(1 for r in robots if r.task_id) / n),
                avg_battery_pct=round(sum(r.battery for r in robots) / n),
                collision_count=self.world.collisions, active_leases=len(leases),
                operational_pct=round(100 * sum(1 for r in robots if r.status != "Blocked" and r.battery > 10) / n),
                completed_total=self.completed_tasks,
                queued_tasks=sum(1 for t in self.tasks.values() if t.status in ("Queued", "Blocked")),
                tasks_per_hour=round(self.session_completed / hours, 1) if hours > 0 else 0.0, tick=self.clock.tick,
            ),
        )
