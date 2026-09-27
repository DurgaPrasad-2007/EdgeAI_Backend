# SIH26123 EdgeFleet: Project Intelligence Dossier

Audit basis: repository at git `781a949` (backend repo, clean tree), all code read directly. Numbers marked **[measured now]** were produced by scripts run during this audit on a developer laptop; nothing was measured on edge hardware. Where the repository does not establish something the text says **NOT VERIFIED IN REPOSITORY**.

Ground rule used throughout: planned ≠ implemented, target ≠ achieved, simulation ≠ real robot, design intent ≠ measurement.

---

## A. PROJECT EXECUTIVE SUMMARY

EdgeFleet is a **software-only, single-process simulation** of a 3-AMR warehouse fleet (plus a Next.js dashboard). Its real technical content is a **shared-nothing multi-agent coordination protocol**: each simulated robot is an independent asyncio agent (`app/agents.py`) that keeps a private, message-derived view of its peers and negotiates node claims, corridor leases, and task auctions over an in-process publish/subscribe bus. There is a decentralised Contract-Net-style auction, node-level reservation with two-node look-ahead, soft-state (heartbeat-expiring) leases, wait-for-cycle deadlock detection with a victim side-step, blockage-triggered A* re-planning, and dropout recovery where a surviving peer hands a silent robot's task back to the mesh.

What it is **not**: it is not deployed on edge hardware, has no ROS 2, no Zenoh, no Space-Time A*, no D* Lite, no machine learning, no robot dynamics, no real safety mechanisms, and no network-fault model beyond total radio silence. The "decentralised" property is real at the **protocol/state level** (no shared reservation table, no shared lock, agents cannot read each other's internals) but **not at the deployment level** (all agents, the message bus, the physics and the task board live in one Python process).

Measured headline (100 fixed seeds, identical per-robot mission queues, only conflict handling differs): **−23.9 % total mission time, −24.3 % makespan, −70.5 % robot-waiting time, 0 collisions in both policies**; median per-run saving 20.5 %; the mesh was slower than the baseline in 14 of 100 runs. The baseline is the *same code with negotiation disabled*, defined by the authors, not an independent traditional implementation.

---

## B. REPOSITORY INVENTORY

Two git repos under `E:\Edge AI\`: `EdgeAI_Backend` (own `.git`, 11 commits, 2026-09-04 → 2026-09-26) and `Edge_AI_Frontend` (no commits inspected). `graphify-out/` is unrelated tool output.

| Area | Files | Purpose |
|---|---|---|
| Robot agents + mesh | `app/agents.py` (~970 lines) | `RobotAgent`, `PeerBus`, `World`, `Peer`, `Auction`, all protocol constants |
| Mesh runtime / observer | `app/sim.py` | `FleetSim`: wires agents+bus+world, task board (announce/dispatch), observer snapshot, KPIs |
| Gateway | `app/coordinator.py` | `FleetCoordinator`: REST-to-mesh translation, write-behind persistence, WS fan-out, live per-robot asyncio tasks |
| World model | `app/graph.py` | 32-node warehouse graph, 3-robot fleet data, `find_shortest_node_path` (A*), `resolve_node` |
| Benchmark | `app/benchmark.py`, `GET /api/benchmark` | headless decentralised vs stop-and-wait comparison |
| API | `app/main.py`, `app/schemas.py`, `app/auth.py`, `app/settings.py` | FastAPI, JWT HS256, roles, rate limit, CORS, WS `/ws/fleet` |
| Persistence | `app/task_store.py`, `app/models.py`, `app/database.py`, `migrations/`, `alembic.ini` | SQLAlchemy async; Postgres+pgvector or SQLite; tasks, audit events, users, knowledge chunks |
| Knowledge search | `app/knowledge.py`, `task_store.search_knowledge` | hashed bag-of-words "embeddings" (lexical, not ML), cosine search |
| Tests | `tests/test_api.py` (14), `test_fleet.py` (11), `test_mesh.py` (10) | 43 pytest cases; last run: 43 passed |
| Deployment | `Dockerfile`, `docker-compose.yml`, `gunicorn_conf.py`, `.github/workflows/ci-cd.yml` | CI: pytest → docker build → GHCR publish; Render-ready |
| Frontend | `Edge_AI_Frontend/src` (Next.js 16.3, React 19) | landing page, digital-twin map, scenario controller, telemetry cards, Mesh Conversation panel, live benchmark section, operator console |
| Docs (mixed reliability) | `README.md`, `SIH26123_SUBMISSION.md`, `SIH_PPT_REFERENCES.md`, `SECURITY_COMPLIANCE.md`, `system_architecture_diagram.html`, `docs/DATABASE.md` | see Section U for which are unsafe |

Not present anywhere: ROS 2 packages/launch/URDF/msg files, Zenoh config, Gazebo/Isaac/Webots worlds, hardware adapters, Jetson/Raspberry Pi deployment files, benchmark CSV/JSON result files, notebooks, replay data, trained-model files. Frontend has one unused dependency (`socket.io-client`; the client uses the native WebSocket).

---

## C. ACTUAL SYSTEM ARCHITECTURE

```
Browser (Next.js)  ──REST/WS──►  FastAPI process (uvicorn/gunicorn)
                                  ├─ FleetCoordinator  (gateway: API→mesh msgs, persistence, WS fan-out)
                                  └─ FleetSim
                                      ├─ PeerBus  (in-process pub/sub; radio-silence switch)
                                      ├─ World    (ground-truth positions, obstacles, collision counter)
                                      ├─ task board ("WMS": announce queued tasks, hear awards/status)
                                      └─ RobotAgent × N  (each: own asyncio control loop + mesh listener)
                                  └─ DB (Postgres/pgvector or SQLite): tasks, audit_events, users, knowledge
```

- **Where decentralisation is real**: `RobotAgent` state (pose, path, claims, task, battery, peer table, auctions, blocked-node belief) is private. Robots learn about each other **only** from `Msg` objects delivered by `PeerBus`. No shared reservation table, no shared lock, no global planner. (`agents.py` `RobotAgent.__init__`, `_on_pose`, `_claimant`, `_all_claimed`.)
- **Where it is centralised**: (1) one OS process hosts everything, so it is a single point of failure in deployment; (2) the **task board** in `FleetSim.dispatch()`/`announce()` and the gateway is the single announcer ("WMS") and holds the authoritative `TaskRecord`s; (3) `World` is global ground truth (collision counting, nearest-robot selection for obstacle reporting); (4) the dashboard reads each agent's `telemetry()` directly.
- **Two run modes**: *live* (`FleetCoordinator._spawn_live`: one clock task + per robot a control loop with phase offset `CONTROL_PERIOD_SECONDS*i/n` and a mesh listener) and *manual clock* (`FleetSim.step()`, deterministic; used by tests and the benchmark).

---

## D. END-TO-END RUNTIME FLOW (what the code actually does)

| # | Stage | Component / file:function | Input → Output | Notes / failure behaviour |
|---|---|---|---|---|
| 1 | Task creation | `main.py` `POST /api/tasks` → `FleetCoordinator.create_task` | `TaskCreate` → in-memory `TaskRecord` (id `TASK-xxxxxxxx`, status `Queued`) | Persisted **behind** the request (write-behind queue `_pending`); a DB failure is logged, not returned to the client |
| 2 | Representation | `schemas.TaskRecord` | pickup, destination (resolved to node ids by `graph.resolve_node`), priority 1–100, payload_kg, payload_size, urgency | Unknown location → 422 |
| 3 | Validation | `FleetSim._check` | task → reason or None | `Blocked` if location unknown, payload > every robot's capacity, or no A* route avoiding blocked aisles |
| 4 | Announcement | `FleetSim.announce` → `bus.publish(TASK_ANNOUNCE, sender "WMS", MESH)` | task payload → all robot inboxes | Ordered by `priority + URGENCY_BONUS{low 0, standard 5, critical 25}`; re-announced if the set of free robots changes or after `REANNOUNCE_TICKS`=4 |
| 5 | Robot state acquisition | `RobotAgent._on_announce` | reads own battery, own position | `is_free()` = online, no task, battery ≥ 20, status ≠ Blocked |
| 6 | Candidate/utility | `RobotAgent._bid` (agents.py:367) | task → score or None | see H |
| 7 | Bidding | `_on_announce` publishes `TASK_BID{score|None}`; peers record it in `Auction.bids` | | Every robot answers (bid or no-bid) |
| 8 | Winner selection | `_try_close` when all live peers have replied, or after `AUCTION_TICKS`=3 | `max((score, robot_id))` | Winner self-assigns and broadcasts `TASK_AWARD`; robots already known busy (`owners`) are excluded |
| 9 | Assignment | `RobotAgent._take` | | leg `to_pickup`; gateway hears the award via the bus tap and sets status `Assigned` (`FleetSim._tap`) |
| 10 | Route generation | `_route_leg` → `_plan(soft=True)` → `graph.find_shortest_node_path` | | Plain A* on static graph; congestion-aware alternate accepted if ≤ 1.35× shortest |
| 11 | Conflict check + reservation | `_move` | | claims own node + next 2 path nodes all-or-nothing; waits if a live peer claims any; zone rule for C-14 |
| 12 | Motion | `_move` | | point robot advances `speed`(20–24 units)/tick along polyline; battery −0.08 %/tick |
| 13 | Telemetry | `publish_pose` (every step) | `POSE` broadcast; `telemetry()` for dashboard | |
| 14 | State update | `FleetSim._tap`, `snapshot()` | | `TASK_STATUS In Progress` at pickup, `Completed` at drop |
| 15 | Completion | `_arrive` | | robot frees itself, then `_send_idle` (charge if <30 %, else home) |
| 16 | Reassignment / replan | see J/P | | triggers: obstacle alert, heartbeat loss (`ORPHAN`), low-battery release, operator revoke |

Timing: control period 0.6 s (`CONTROL_PERIOD_SECONDS`); messages are delivered synchronously (zero latency, lossless, ordered) inside `PeerBus.publish`/`pump`.

---

## E. IMPLEMENTATION TRUTH TABLE

Legend: A implemented+verified (by tests/measurement in repo), B implemented not verified, C partial, D mocked/simulated, E configured unused, F planned, G not present.

| Capability | Class | Evidence |
|---|---|---|
| Multi-robot coordination | **A** | `agents.py`; `tests/test_mesh.py::test_mesh_is_collision_free_and_never_deadlocks[6 seeds]`; soak runs (200 seeds, 0 collisions) |
| Decentralised communication (protocol) | **A/D** | `PeerBus` in `agents.py`; semantics real, transport in-process (D) |
| Peer-to-peer over a real network | **G** | no socket/DDS/Zenoh code |
| Task allocation | **A** | `_bid`, `_try_close`; `test_live_mode_runs_every_robot_as_its_own_asyncio_task` |
| Contract-Net (announce/bid/award) | **A** | `_on_announce/_on_bid/_try_close/_on_award`; message types TASK_ANNOUNCE/BID/AWARD |
| Utility / scoring | **A** | `agents.py:367` `_bid` (fixed weights) |
| Priority handling | **A** | task priority in bid (+0.25×), urgency bonus in announce order, robot rank for mutex |
| Battery-aware dispatch | **A** | bid term 0.40×battery; `MIN_BID_BATTERY` 20 excludes bidders |
| Charging logic | **C/D** | `_send_idle` → CHARGE node; +1.5 %/tick; no charger contention/queueing model |
| Path planning (A*) | **A** | `graph.find_shortest_node_path` |
| Space-Time A* | **G** | no time dimension anywhere; claim is in `system_architecture_diagram.html` only |
| D* Lite | **G** | README says so explicitly |
| Dynamic obstacles | **C** | operator-injected blocked *nodes* only; no moving obstacles |
| Shared-space conflict detection | **A** | node-claim conflicts (`_claimant`, `_move`) |
| Reservation system | **A** | `claims` (own node + 2 ahead), broadcast in POSE |
| Corridor leasing | **A** | C-14 claim; soft-state expiry `LEASE_TICKS`=8 (4.8 s) after last heartbeat |
| Deadlock detection | **A** | `_deadlock_cycle` on wait-for edges from POSE |
| Deadlock recovery | **A** | `_side_step_path` BFS ≤4 hops + `_patient_detour`; regression seeds 0,2,27,32,34 |
| Starvation prevention | **C** | no aging; only `stuck` counter that disables the intent rule after 12 ticks and forces recovery |
| Rerouting | **A** | `_replan`, `_apply_obstacle`; `test_blockage_reroutes_and_mission_still_completes` |
| Dynamic task reassignment | **A/C** | only on: heartbeat loss, unloaded low-battery, operator revoke/directive, rejoin. Loaded robots never hand off |
| Peer state synchronisation | **A** | POSE every tick; `SYNC_REQ/SYNC` for blocked set on rejoin |
| Stale-state handling | **A** | `_alive` (8-tick timeout), `_expire_peers`, `_ghosts` |
| Network failure handling | **C/D** | only all-or-nothing radio silence (`go_silent`, `PeerBus.silenced`); no delay/loss/partition model |
| Robot failure handling | **A/D** | simulated dropout: peers expire claims, `ORPHAN` → requeue → re-auction; robot rejoins after 30 ticks |
| Localization failure | **G** | poses are exact |
| Safety stop | **D** | dropout sets status `Blocked` (simulated stop); not a safety function |
| Collision prevention | **A (simulated)** | reservation logic + proximity monitor `World.check_collisions` (radius 10 units) |
| Edge execution | **G** | nothing runs on edge hardware; only "runs as separate asyncio tasks" |
| Raspberry Pi / Jetson support | **F** | UI/docs text and `README` "production deployment boundary" only |
| ROS 2 | **G** (README: "no ROS 2/Zenoh transport") |
| Zenoh | **G** |
| Simulator | **D** | own kinematic point-robot simulator (`agents.py` `World`, `_move`) |
| Simulated robots | **D** | 3 `RobotSpec`s in `graph.ROBOT_FLEET` |
| Digital twin / dashboard | **A** | Next.js map + telemetry, fed by `snapshot()` |
| Telemetry | **A** | `RobotState` incl. `decision`, `online` |
| Database | **A** | tasks + audit + users + knowledge; write-behind |
| Authentication | **A** | JWT HS256, roles; **demo shortcut**: guests get viewer/operator/fleet-agent on demo endpoints (`auth.demo_identity`) |
| Security hardening | **C** | headers, TrustedHost, CORS allow-list, 240 req/min/path limiter (in-memory); no external pen-test |
| Metrics | **A** | `Kpis`, `benchmark.compare` |
| Benchmarking | **A/C** | `app/benchmark.py`; baseline is a variant of same code |
| Experiment automation | **C** | benchmark endpoint + pytest; no experiment tracking |
| Logging / audit | **A** | `audit_events` table, `[AUDIT]` logs |
| Replay | **G** | |
| APIs | **A** | REST + WS |
| Deployment | **C** | Docker, Render config, CI publish to GHCR; no evidence it was deployed |

---

## F. COMPONENT-BY-COMPONENT CODE EVIDENCE

- **`agents.py:98 PeerBus`**: per-robot `deque` inbox, `publish` (delivers to all except sender, honours `silenced`, taps for observers), `pump` (synchronous drain).
- **`agents.py:146 World`**: `pos`, `docked`, `parked`, `obstacles`, `collisions`, `staging` set; `check_collisions` counts pairs closer than `COLLISION_RADIUS`=10 units, **skipping pairs where either robot is "parked"** (standing on a dock/home/charge node without claiming it).
- **`agents.py:182 Peer`**: soft-state view (last_seen, claims, occupied nodes, status, battery, priority, wait_for, path_nodes, intent).
- **`agents.py:217 RobotAgent`**: `step()` (898): drain inbox → expire peers → close stale auctions → replan if Blocked → undock → `_move` → charge → sync leases → publish POSE. `run()` = own loop; `listen()` = mesh listener.
- **`sim.py FleetSim`**: `dispatch`, `announce`, `_tap` (task-board bookkeeping from heard messages), `report_blockage`, `reset`, `snapshot`.
- **`coordinator.py FleetCoordinator`**: translates REST into bus messages (`TASK_REVOKE`, `TASK_DIRECTIVE`, `TASK_UPDATE`), persistence (`_flush` → `TaskStore.commit`, single transaction), `_publish` to WS queues.
- **`benchmark.py`**: `workload(seed)`, `run_once(policy, seed)`, `compare()`.
- **`graph.py`**: 32 nodes, `MUTEX_ZONES={"C-14"}`, `ROBOT_FLEET` = AMR-01 Atlas (1200 kg, speed 20, home DOCK-W, 96 %), AMR-02 Nova (350 kg, 24, INT-N1, 90 %), AMR-03 Kite (700 kg, 20, DOCK-E, 94 %).

---

## G. DISTRIBUTED COORDINATION AUDIT

1. **Is coordination decentralised?** Protocol/state: yes. Deployment: no (single process).
2. **Central coordinator?** No central *planner*. There is a central **task board/announcer** (`FleetSim`, "WMS") and a **gateway** holding authoritative task records and DB.
3. **Who owns global state?** `World` (ground truth) and `FleetSim.tasks` (task records). Robots own their own pose/path/claims.
4. **Independent decisions:** each robot decides its own bids, claims, waits, detours, side-steps.
5. **Discovery:** each robot publishes `POSE` at construction (`FleetSim._build` publishes every agent's pose then pumps); peers appear in `RobotAgent.peers` on first POSE.
6. **State exchange:** `POSE` broadcast every control tick containing: x, y, node, claims, occupied nodes, status, battery, priority, wait_for, wait_node, remaining path nodes, intent_zone, intent_hops, docked.
7. **Peer-to-peer?** Logically yes (broadcast/direct on a bus with no broker role); physically in-process.
8. **Protocol:** custom in-process message bus (`Msg`). Not ROS 2, not Zenoh, not DDS, not MQTT.
9. **Message types (implemented):** POSE, MUTEX_REQ, MUTEX_GRANT, MUTEX_RELEASE, YIELD_ACK, OBSTACLE_ALERT, AGENT_FAULT, TASK_ANNOUNCE, TASK_BID, TASK_AWARD, TASK_STATUS, TASK_REVOKE, TASK_DIRECTIVE, TASK_UPDATE, ORPHAN, SYNC_REQ, SYNC, HEARTBEAT (battery alert).
10. **Frequency:** POSE once per robot per control tick (0.6 s). Other messages event-driven.
11. **Delayed message:** not modelled (delivery is synchronous). NOT VERIFIED IN REPOSITORY.
12. **Lost message:** not modelled. NOT VERIFIED IN REPOSITORY.
13. **Unreachable robot:** peers mark it dead after `HEARTBEAT_TIMEOUT`=8 ticks (4.8 s); its non-physical claims lapse; its physically occupied nodes become "ghost" obstacles (`_ghosts`) that others cannot enter or route through; the lowest-id survivor publishes `ORPHAN` for its task.
14. **Operate without backend?** In-process: robots already holding tasks continue negotiating if the gateway/task-board stops calling `dispatch` (`test_mesh_keeps_moving_and_negotiating_without_the_gateway`). In real deployment terms: NOT VERIFIED (no separate process/host).
15. **What is genuinely local?** Bidding, claiming, yielding, replanning, deadlock resolution, dropout detection.

Message volume [measured now]: 3 robots ≈ 3.2 messages/tick, 6 robots ≈ 6.5, 10 robots ≈ 11.0 published (each delivered to n−1 inboxes → O(n²) deliveries).

---

## H. TASK ALLOCATION AUDIT

- **Exact utility** (`agents.py:367`):
  `score = (max_payload_kg − task.payload_kg) × 0.05 + battery% × 0.40 − A*_distance(robot position → pickup) × 0.08 + task.priority × 0.25`, `None` if payload > capacity or no route to pickup.
  Note: it **omits the pickup→drop leg**, and priority is a task constant so it does not differentiate robots for one task (it matters only across tasks).
- **Weights:** fixed literals inside `_bid`; not configurable, not learned.
- **Priority:** integer 1–100 on the task; announce order uses `priority + URGENCY_BONUS`; robot mutex rank = `priority + 0.2×(100 − battery)`.
- **Battery:** bid term; bidders <20 % excluded; idle robots <30 % go to CHARGE; charge to 95 %.
- **Payload:** hard filter (bid `None`); tasks nobody can carry → `Blocked` at `FleetSim._check`.
- **Winner:** `max((score, robot_id))`, so **on an exact tie the higher id wins**; but the double-award tie-break in `_on_award` makes the **higher id release** (lower id keeps). These two rules are inconsistent (rare edge case). The dashboard text "lower ID breaking ties" (mutex ranking) contradicts the code (`rank` tuples compare higher id larger).
- **No bidder:** task stays `Queued`; re-announced when the free set changes or every 4 ticks.
- **Winner failure:** heartbeat loss → ORPHAN → task requeued → re-auction (tested live in `test_silent_robot_claims_expire_and_its_task_is_reassigned`).
- **Active task reassignment:** yes but only (a) dead peer, (b) unloaded (`leg=="to_pickup"`) robot falling below 30 % battery (`set_battery`), (c) operator revoke/directive, (d) robot rejoining after dropout. A robot carrying cargo never hands off.
- **Fairness/optimality:** greedy per-task auction; no global optimisation; no bundle/combinatorial bidding.

---

## I. PATH PLANNING / MAPF AUDIT

- **Planner:** A* (`graph.find_shortest_node_path`, `heapq`, f = g + Euclidean h, edge cost Euclidean). **Not Space-Time A***: state is a graph node id, no time index, no time-expanded graph, no constraint table.
- **Resolution:** spatial = graph nodes (32 nodes, edges 70–180 map units); temporal = control tick 0.6 s, used only for timeouts/leases, not for planning.
- **Obstacles:** operator-blocked nodes, plus "ghost" nodes of silent peers. Other live robots are handled by **claims** (reservation), and softly by an alternative-route search that excludes peer-claimed nodes if the alternative is ≤ 1.35× longer (`_plan(soft=True)`, `SOFT_DETOUR_FACTOR`).
- **Footprint:** robots are points; collision radius 10 map units used only by the monitor.
- **Constraints:** none in the planner; safety comes from run-time claims.
- **Complexity:** O(E log V) per plan; **[measured now]** mean 7 µs, p95 16 µs, max ≈ 0.3–0.45 ms over 20,000 random pairs on the 32-node graph, on a developer laptop (not edge hardware).
- **MAPF status:** this is **decoupled prioritised reservation with local negotiation**, not a MAPF solver (no CBS, no ICTS, no windowed HCA*). The references file cites MAPF for conflict definitions and makespan only.
- **Vertex conflicts:** prevented by node claims. **Edge/swap conflicts:** prevented indirectly (a robot standing on a node owns it, so a head-on swap cannot start). No explicit edge-conflict data structure.

---

## J. CONFLICT / DEADLOCK / RESERVATION AUDIT

- **Conflict** = a node needed within the next 2 path nodes is claimed by a live peer, or an occupied/ghost node. Checked **before moving** each tick (`_move`).
- **Reservation:** `claims` = current node + up to `LOOKAHEAD_NODES`=2 path nodes; taken all-or-nothing; broadcast in POSE; released as the robot advances (`_prune`). Docked robots publish no claims and are invisible on lanes.
- **Lease (C-14):** claiming the zone node is the lease. Expiry is **soft-state**: peers stop honouring claims of a robot silent for >8 ticks. `LEASE_TICKS`=8 (4.8 s) is a chosen constant, equal to the heartbeat timeout; no derivation from speeds/geometry is recorded. Manual API leases (`request_zone`) use a caller-given TTL.
- **Right of way at C-14:** `_outranked`: a live peer with intent for the same zone that is closer-or-equal in hops **and** higher `rank` wins; otherwise first-claimer wins. The rule is ignored after `stuck ≥ 12`.
- **Head-on on a lane:** the robot standing on the node owns it; the other waits; mutual wait → cycle detection.
- **3-/4-way intersections:** handled by the same node-claim mechanism (no special junction logic). Only C-14 is a "mutex zone".
- **Overlapping reservations:** prevented by exclusive claims among live peers; in-process delivery is synchronous, so two robots cannot claim in the same instant. **Under real message delay this is NOT VERIFIED** (there is a tie-break comment but no delay tests).
- **Exceeding a reservation:** no concept; a robot only holds claims while it is alive and moving.
- **Deadlock detection:** wait-for graph from POSE `wait_for`; `_deadlock_cycle` (agents.py:715). Triggered every `deadlock_ticks` (12; baseline 40) of continuous blocking.
- **Recovery:** every member computes the same victim (lowest rank among robots that have a free parking path); only the victim moves (`_side_step_path` BFS up to 4 hops, may pass through unclaimed nodes on others' routes; holds `HOLD_TICKS`=15 + 2×hops). If no cycle, `_patient_detour` (after 3 blocked ticks, ≤ 14 extra ticks, 20-tick cooldown) or a plain detour.
- **Starvation:** no aging; a low-rank robot can be repeatedly outranked at the zone until its `stuck` counter ≥ 12 disables the intent rule. No proof of bounded waiting; empirical only.
- **Repository-backed deadlock scenario:** two robots in adjacent dead-end racks (RACK A-01 / RACK B-01) each needing the other's rack; resolved by one parking beyond the junction (found by seed 32; regression in `tests/test_mesh.py`).

---

## K. EDGE COMPUTE AUDIT

- Runs on: a developer laptop / a Render container (Docker/gunicorn). **No edge device is used anywhere.**
- Hardware named in text: Raspberry Pi 5, Jetson Orin (README "production boundary", HTML diagram, UI copy). No code, config, or measurement.
- CPU/RAM requirement: NOT VERIFIED IN REPOSITORY. [measured now] per-tick coordination cost on a laptop: 0.07 ms mean at 3 robots, 0.12 ms at 6, 0.5 ms at 10, all robots in one process.
- Backend dependencies: DB (tasks/audit/users/knowledge), FastAPI gateway, dashboard. The **coordination loop** itself has no dependency on DB or network (in-process).
- Real vs simulated: everything about robots is simulated.

---

## L. ROS2 / SIMULATION / ROBOTICS AUDIT

- ROS 2: **absent** (no `rclpy`, no packages). Distribution, nodes, topics, QoS, actions/services: **none**.
- Simulator: the repo's own kinematic simulator: robots follow polyline paths at a constant per-tick speed; pose is exact.
- Robot model / differential drive / velocity commands: **not implemented**. No wheel odometry, no controller, no localisation.
- Collisions: proximity monitor only (10 map units).
- Gazebo/Isaac/Webots: none.

---

## M. AI / EDGE-INTELLIGENCE AUDIT

- Trained ML model: **none**. Inference: **none**. RL: **none**.
- What exists: **rule-based, heuristic distributed decision-making** (scoring function with fixed weights, deterministic ranking, A*, wait-for-cycle detection, threshold-driven behaviours) and **adaptive behaviour by rule** (congestion-aware detour, dropout recovery).
- "Knowledge search/RAG": deterministic hashed bag-of-words vectors with cosine similarity (README: "lexical, not semantic"). Not an AI model.
- Most defensible wording: **"edge-executable, decentralised multi-agent coordination using rule-based heuristics and graph search"**. Do **not** say "AI-powered", "machine learning", "neural", "learned policy".
- Why edge would matter (design argument, not measured here): no round trip to a server for right-of-way decisions; survive backend loss. What is *demonstrated*: in-process robots keep negotiating when the task board stops.

---

## N. SAFETY / ISO CLAIM AUDIT

**Actually implemented (simulation-level):** two-node all-or-nothing claims; exclusive node ownership; heartbeat-timeout expiry of claims; ghost-node avoidance for silent robots; simulated safety stop on dropout; proximity monitor with a 10-unit threshold; guaranteed no lease for docked robots' lanes.
**Not implemented:** emergency stop, protective/warning LiDAR fields, speed limiting, braking model, footprints, safety-rated controller/PLC, watchdog on real hardware, functional-safety validation (IEC 61508/13849), hazard analysis, verification evidence.
**ISO 3691-4:** `SECURITY_COMPLIANCE.md` explicitly says it is not a certification; landing copy now says "design reference". **DO NOT claim compliance, conformance, "aligned to", "engineered to" or "0.5 m envelope" as fact.**
Safe wording: "reservation-based collision avoidance validated in simulation with zero proximity violations in N runs; not a safety-rated system."
Dangerous wording to avoid: "ISO 3691-4 compliant", "safety-certified", "SIL-2", "industrial-grade safety", "collision-free" (unqualified), "deadlock-free" (unqualified).

Note on the collision metric: the monitor exempts robots parked on dock/home/charge nodes, and robots are points. "0 collisions" means "0 proximity violations by this monitor".

---

## O. PERFORMANCE / BENCHMARK AUDIT

**Measured (in repository code, reproducible):** `app/benchmark.py`, `GET /api/benchmark` (`FleetCoordinator.compare_policies`), cached.

Design: 3 robots; per robot 4 missions (12 total) crossing C-14 or the N-S spine; both policies receive **identical assignments** (directives); 100 fixed seeds `range(1,101)`; deterministic; time = ticks × 0.6 s; tick cap 4000.

| Metric (mean per run) | Stop-and-wait | Decentralised | Change |
|---|---|---|---|
| Sum of mission completion times | 679.4 s | 516.8 s | **−23.9 %** |
| Fleet makespan | 253.1 s | 191.7 s | −24.3 % |
| Robot-time spent yielding | baseline | | −70.5 % |
| Collisions | 0 | 0 | |
| Median per-run saving | | | 20.5 % |
| Runs where mesh slower | | | 14 / 100 |

Calculation: (679.4 − 516.8) / 679.4 = 23.9 %.

Honest caveats: (1) the baseline is the *same agent code with negotiation switched off* (`sw=True`: no intent rule, no congestion-aware routing, deadlock timeout 40 vs 12, no patient detour; it still uses 2-node claims), defined by the authors; the first baseline variant I tried deadlocked and was changed; (2) decentralised parameters (`PATIENCE_TICKS`=3, `DETOUR_BUDGET_TICKS`=14, `SOFT_DETOUR_FACTOR`=1.35) were tuned on seeds 100–139 and validated on 1000–1039 (24.5 % / 24.4 %) before the seed set 1–100 was fixed; a 60-seed slice (1–60) gave 19.8 % for mission time, so the margin over 20 % is thin and slice-dependent; (3) one warehouse layout, 3 robots, synthetic workload; (4) no confidence intervals or std-dev are reported; (5) simulated time only.
Other measured **[now]**: A* mean 7 µs; message rates above; scaling below.

**Scaling [measured now]** (random workload, 15 tasks/robot released every 20 ticks, 2500 ticks; fleet extended by data):

| Robots | Tasks done / issued (2500 ticks) | Collisions | Yields / detours / side-steps | tick ms mean/p95 |
|---|---|---|---|---|
| 3 | 45 / 45 | 0 | 78 / 33 / 7 | 0.07 / 0.17 |
| 6 | 90 / 90 | 0 | 242 / 118 / 5 | 0.12 / 0.19 |
| 10 | 61 / 125 | 0 | 1366 / 1067 / 98 | 0.50 / 0.85 |
| 10 (6000 ticks) | 150 / 150 | 0 | 3187 / 2421 / 232 | 0.59 / 0.99 |

Not measured anywhere: communication latency, planning latency on hardware, throughput in tasks/hour on a real layout, idle-travel, battery impact, near-conflict counts, std-dev/variance, replans per run (counters exist only as event text).

**Estimated/claimed but NOT measured (do not use):** "p95 < 150 ms", "~84 ms", "<15 ms bidding", "3.2× faster", "+27.3 % throughput", "<12 W", "<120 MB", "42 ms D* Lite replan". (Landing copy was corrected; `system_architecture_diagram.html` and some legal/marketing pages still contain them.)

---

## P. FAILURE-MODE ANALYSIS

| Situation | Behaviour | Evidence |
|---|---|---|
| Aisle blocked | reporter (nearest robot) broadcasts; every robot whose remaining path crosses it re-plans (A*); tasks with no route become `Blocked` | `report_blockage`, `_apply_obstacle` |
| Robot goes silent (30-tick blackout) | peers expire claims after 4.8 s; block its physical nodes; ORPHAN → task re-auctioned; robot rejoins, asks `SYNC_REQ` | `go_silent`, `_expire_peers`, live browser run |
| No route | robot status `Blocked`, retries every 3 ticks | `step` |
| Low battery, unloaded | releases task, goes to charge | `set_battery` |
| Low battery, loaded | completes delivery then charges | `_arrive` → `_send_idle` |
| Mutual wait | cycle detected → victim parks | tests |
| Gateway/DB slow | commands answered from memory; persistence write-behind (fixed after finding 1.6 s/db-call remote latency) | `test_commands_do_not_wait_for_a_slow_database` |
| Dashboard reset | seq monotonic (fixed) | `test_snapshot_sequence_never_goes_backwards_across_reset` |
| Message delay/loss/partition | NOT MODELLED | |
| Localization error | NOT MODELLED | |
| Process crash | everything stops; in-flight tasks re-queued on restart (`requeue_orphans`) | |

Biggest technical risks: (1) results depend on synchronous lossless in-process messaging; (2) benchmark baseline is self-defined; (3) congestion behaviour at ≥10 robots (yield/detour counts grow ~40×, throughput drops).
Brittle/hard-coded: single 32-node graph, one mutex zone, fixed weights, fixed speeds/drain/charge rates, tick-based timeouts, staging-node collision exemption, guest demo permissions.

---

## Q. SCALABILITY ANALYSIS

- Messages: O(n) publishes per tick, O(n²) deliveries and O(n²) peer-table updates.
- Planner cost is negligible (µs) on this small graph; the limiter is **spatial congestion** on a 32-node graph, not compute: at 10 robots the fleet completed only 61/125 tasks in 2500 ticks, though all 150/150 in 6000 ticks with 0 collisions.
- First thing to fail at 10 robots: throughput/latency (yield & detour storms), not safety. First thing to fail on a real floor: message delay/loss, localisation error and robot dynamics, none of which are modelled.

---

## R. DEMO SCENARIO PLAN (only what the code performs)

| Demo | Trigger | Expected | Metric | Evidence / failure mode |
|---|---|---|---|---|
| Normal allocation | Scenario 3 (Auction) | 4 tasks, every robot bids on each, winners by score | Mesh Conversation: TASK_ANNOUNCE / BID / NO_BID / AUCTION_WIN | `landing-page.tsx` scenario; fail: heavy tasks only fit AMR-01 |
| Contention + narrow aisle | Scenario 1 (Mutex) | AMR-01/03 head-on at C-14: one leases, other yields at hold line | "LEASE_ACQUIRED", "yielded to … rank", collisions 0 | `_outranked`; screenshot verified |
| Blocked aisle | Scenario 2 (Obstacle) | OBSTACLE_DETECTED broadcast, robots re-plan | REROUTE events | timing uses 2.5 s front-end delay |
| Robot unavailable | Scenario 4 (Dropout) | victim OFFLINE, lease lapses, peer takes task | OFFLINE badge, ORPHAN, re-award | recovers after 30 ticks |
| Low battery | Scenario 5 | victim drops to 22 %, hands job back, charges | decision text, re-auction | only if victim not yet loaded |
| Benchmark vs baseline | `#benchmarks` section | live −23.9 % with honest caveats | numbers above | first load ≈ 10 s |
Not supported: communication degradation (only total silence), moving obstacles, localisation failure, real hardware, ROS 2.

---

## S. SENIOR REVIEWER ATTACK QUESTIONS

| Question | What answers it | Risk |
|---|---|---|
| Where is this actually running on an edge device? | nowhere | HIGH: must say "edge-executable design, simulated" |
| Is it decentralised if it's one process? | agents share nothing but a bus; test without gateway | MED: be explicit about in-process transport |
| Show Space-Time A* | none (plain A* + reservations) | HIGH if claimed |
| Show ROS 2 / Zenoh | none | HIGH |
| Why should the baseline be believed? | same code, flags in `RobotAgent.__init__` | MED-HIGH: self-defined baseline |
| Is 20 % robust? | 23.9 % over 100 seeds; 19.8 % on seeds 1–60; median 20.5 %; 14 % of runs worse | MED |
| What if messages are lost/late? | not modelled | HIGH |
| Prove deadlock-free | empirical (200 random seeds) + regression seeds; no proof | MED |
| Starvation? | no aging | MED |
| Your bid ignores pickup→drop distance | true | LOW-MED |
| Collision definition? | points, 10 units, parked exemption | MED |
| Scale to 10+ robots? | completes but congested (table Q) | MED |
| ISO 3691-4? | not compliant | HIGH if claimed |
| Edge AI? where is the AI? | rule-based, no ML | HIGH if claimed |
| Numbers on the landing page? | corrected to measured/design-target wording | MED (other pages still stale) |
| Guests can control the fleet? | demo shortcut in `auth.demo_identity` | MED (security reviewers) |
| Reproduce the benchmark | `python -m app.benchmark` | LOW |

---

## T. CLAIMS WE CAN SAFELY MAKE

- "Decentralised multi-agent coordination simulated with three independent asynchronous robot agents that share no state and coordinate only through messages."
- "Contract-Net-style task auction run by the robots (announce → bid/no-bid → award), no central dispatcher choosing winners."
- "Node-level reservation with two-node look-ahead and soft-state corridor leases that expire when a peer's heartbeat is lost (4.8 s in simulation)."
- "Wait-for-cycle deadlock detection with deterministic victim selection and local side-step recovery."
- "A* re-planning on blockage alerts broadcast on the mesh; congestion-aware detours."
- "Zero proximity violations (10-unit threshold) in 100 benchmark runs and 200 random-workload runs."
- "In a controlled simulation with identical assignments, −23.9 % total mission completion time vs a stop-and-wait baseline (median 20.5 %, mesh slower in 14 % of runs; baseline defined by the authors)."
- "A simulated robot dropout is detected by peers and its task is re-auctioned and completed."
- "Live dashboard shows each robot's own decision text and the actual robot-to-robot messages."

## U. CLAIMS WE MUST NOT MAKE

**DO NOT PUT THIS CLAIM IN THE DECK:** Space-Time A*; D* Lite; ROS 2 / Zenoh / DDS in this implementation; running on Raspberry Pi/Jetson; "<15 ms", "p95 < 150 ms", "84 ms", "<12 W", "<120 MB"; "+27.3 %"; "3.2× faster"; ISO 3691-4 compliance/alignment; SIL-2; "AI/ML/neural/learned"; "deadlock-free" or "collision-free" unqualified; "production-ready"; "hardware-validated"; "fault-tolerant network"; "proven scalable to N robots"; "Ed25519 / signed Zenoh tokens" (security page, HTML diagram); any statement that the utility formula is `0.4P − 0.35D + 0.25(B−20 %)` (HTML diagram; real formula in H).
Unsafe sources in the repo: `system_architecture_diagram.html` (fabricated components/formula), `README.md` sections "What is implemented" (pre-mesh wording: says coordinator arbitrates), frontend pages `app/docs`, `security`, `privacy`, `terms`, `bento-grid`, footer (mention Zenoh/ROS 2/ISO as if in use).

---

## V. TOP 10 TECHNICAL IMPROVEMENTS

1. Move each `RobotAgent` to its own process/transport (Zenoh or ROS 2 topics) and re-run the same tests, exposing real latency.
2. Add a network model to `PeerBus` (delay, loss, reordering, partitions) and test claim races and lease expiry under it.
3. Implement genuine time-indexed reservations (Space-Time A*/windowed) or drop the term.
4. Independent baseline (e.g., centralised intersection lock, or Open-RMF-style) and confidence intervals over more layouts.
5. Include pickup→drop distance and queue load in the bid; make weights configuration.
6. Aging/fairness guarantee and a formal progress argument for deadlock recovery.
7. Robot dynamics + footprint + real collision geometry; remove parked exemption from the metric.
8. Hardware-in-the-loop measurement on a Pi/Jetson (CPU, RAM, latency).
9. Charging model (contention, queueing, energy-aware task sizing).
10. Correct all stale docs/pages; replace demo guest privileges with scoped demo tokens.

---

## W. ~100 TRUTHFUL ATS / TECHNICAL KEYWORDS

Confidence H = directly implemented/tested; M = implemented in simulation only; L = design intent (avoid).

**Robotics:** autonomous mobile robots (AMR) [H] · warehouse robotics [H] · kinematic simulation [M] · multi-robot systems [H] · robot fleet [H] · docking/charging behaviour [M] · point-robot model [M] · digital twin [M] · robot telemetry [H].
**Multi-agent systems:** multi-agent coordination [H] · autonomous agents [H] · agent-based simulation [H] · shared-nothing agents [H] · asynchronous agents [H] · peer-to-peer coordination [M] · local decision-making [H] · negotiation protocol [H] · right-of-way arbitration [H] · soft-state protocol [H] · heartbeat failure detection [H].
**Fleet management:** fleet dashboard [H] · task queue [H] · mission lifecycle [H] · KPI monitoring [H] · audit trail [H] · operator console [H].
**Task allocation:** Contract Net Protocol [H] · task auction [H] · utility-based bidding [H] · decentralised task allocation [H] · battery-aware task assignment [H] · payload-aware assignment [H] · priority scheduling [H] · dynamic task reassignment [H/C] · orphaned-task recovery [H].
**Path planning:** A* search [H] · graph-based path planning [H] · dynamic re-planning [H] · congestion-aware routing [H] · obstacle avoidance (blocked-node) [H] · look-ahead reservation [H] · detour planning [H].
**Distributed systems:** decentralised architecture [H] · publish/subscribe [M] · message bus [H] · eventual state convergence (POSE) [M] · lease-based mutual exclusion [H] · distributed mutual exclusion [M] · deadlock detection [H] · wait-for graph [H] · deadlock recovery [H] · fault injection [H] · failure recovery [H] · write-behind persistence [H].
**Edge computing:** edge-executable design [M] · edge-first architecture [M/L] · low-latency local decisions [M: not measured on hardware] · offline-capable coordination [M].
**ROS2:** (no supported terms; ROS 2 = DO NOT USE except "future integration target").
**Communication:** WebSocket telemetry [H] · REST API [H] · JWT authentication [H] · role-based access control [H] · in-process message bus [H] · broadcast/unicast messaging [H].
**Simulation:** discrete-time simulation [H] · deterministic simulation [H] · headless benchmarking [H] · fault injection (radio dropout, blockage, low battery) [H] · seeded randomised workloads [H] · stress/soak testing [H].
**Optimization:** heuristic optimisation [M] · makespan reduction [H] · waiting-time reduction [H] · throughput analysis [M].
**Safety (careful):** collision avoidance [M] · reservation-based safety [M] · proximity monitoring [M] · safe yielding [M] · fail-safe stop (simulated) [M]. **DO NOT USE:** ISO 3691-4 compliant, SIL, certified, functional safety.
**Metrics:** total mission completion time [H] · makespan [H] · robot waiting time [H] · proximity violations [H] · queue length [M] · A* planning latency (laptop) [H].
**Deployment:** Docker [H] · CI/CD (GitHub Actions) [H] · GHCR publishing [H] · Gunicorn/Uvicorn [H] · Render deployment configuration [M] · Alembic migrations [H].
**Software engineering:** FastAPI [H] · asyncio [H] · SQLAlchemy async [H] · PostgreSQL / pgvector [H] · SQLite [H] · Next.js / React [H] · TypeScript [H] · pytest [H] · property/randomised testing [H] · write-behind caching [H] · rate limiting [H] · CORS/TrustedHost hardening [H].
**Warehouse automation:** smart warehouse [H] · narrow-aisle/choke-point management [H] · aisle blockage handling [H] · charge-bay management [M] · pick-and-drop missions [H].
**DO NOT USE:** Space-Time A*, D* Lite, Zenoh, ROS 2 (as implemented), DDS, Jetson-validated, Raspberry Pi-validated, machine learning, deep learning, reinforcement learning, computer vision, SLAM, EKF, LiDAR fusion, Ed25519, ORCA (implemented), CBS/MAPF solver, digital thread, ISO-certified.

## X. KEYWORD → EVIDENCE MAP (representative)

| Keyword | Evidence (file:function) | Conf. |
|---|---|---|
| Contract Net Protocol | `agents.py` `_on_announce/_on_bid/_try_close/_on_award` | H |
| decentralised task allocation | above + `sim.py::FleetSim._tap` (observer only) | H |
| lease-based mutual exclusion | `agents.py::zone_claims/_sync_leases/_outranked` | H |
| wait-for graph deadlock detection | `agents.py::_deadlock_cycle`, `_wait_edge` | H |
| A* search | `graph.py::find_shortest_node_path` | H |
| congestion-aware routing | `agents.py::_plan(soft=True)` | H |
| heartbeat failure detection | `agents.py::_alive/_expire_peers` | H |
| orphaned-task recovery | `ORPHAN` in `_expire_peers`; `sim.py::_requeue` | H |
| fault injection | `go_silent`, `set_battery`, `report_blockage` | H |
| headless benchmarking | `benchmark.py` | H |
| write-behind persistence | `coordinator.py::_flush`, `task_store.py::commit` | H |
| JWT / RBAC | `auth.py` | H |
| digital twin dashboard | `Edge_AI_Frontend/src/components/digital-twin/*` | M |
| edge-executable | agents run as independent tasks; no hardware run | M |
| Space-Time A* / Zenoh / ROS 2 | absent | DO NOT USE |

---

## Deck-concept mapping (Section 14)

- **Title/problem:** decentralised coordination of ≥3 AMRs; goal: zero collisions + ≥20 % lower completion time vs stop-and-wait; simulation-based.
- **Decision spine:** robot-run auction; local claims/leases; local recovery.
- **Technical approach:** shared-nothing agents, peer message bus, Contract-Net auction, 2-node look-ahead reservation, soft-state leases, wait-for-cycle deadlock recovery, A* re-planning, congestion-aware detours.
- **Architecture:** agents + bus + world + task board + gateway + dashboard (state plainly it is one process; transport swappable).
- **Feasibility/viability:** per-tick cost 0.07 ms (3 robots, laptop) and 7 µs A* on this graph; edge hardware not yet measured (future work).
- **Impact:** −23.9 % mission time and −70 % waiting in simulation, with caveats.
- **Validation:** 43 tests, 200-seed soak with blockages, 100-seed benchmark, live browser run.
- **Research:** Open-RMF (architecture inspiration), MAPF (conflict definitions/makespan), DC-MRTA (decentralised bidding), ORCA (principle only, not implemented).
- **Demo:** the five scenarios plus live Mesh Conversation and benchmark.

---

## Y. FINAL PROJECT FACT SHEET (compact)

- **Purpose:** simulate decentralised coordination of 3 warehouse AMRs; dashboard shows fleet, robot decisions and the messages they exchange.
- **Architecture:** FastAPI process; `RobotAgent`×3 asyncio agents; in-process `PeerBus`; `World` physics; task board; Next.js dashboard; SQLite/Postgres persistence (write-behind).
- **Algorithms:** A* on a 32-node graph; utility bid `0.05·(capacity−payload) + 0.40·battery − 0.08·dist_to_pickup + 0.25·priority`; 2-node look-ahead claims; soft-state leases (TTL 8 ticks = 4.8 s); intent-based right-of-way at C-14; wait-for-cycle deadlock recovery (victim side-step, BFS ≤4 hops); patient detour (≥3 blocked ticks, ≤14 extra ticks, 20-tick cooldown); heartbeat-timeout failure detection; ORPHAN task recovery.
- **Communication:** in-process pub/sub; message types POSE, MUTEX_*, YIELD_ACK, OBSTACLE_ALERT, AGENT_FAULT, TASK_ANNOUNCE/BID/AWARD/STATUS/REVOKE/DIRECTIVE/UPDATE, ORPHAN, SYNC(_REQ); POSE every 0.6 s; synchronous, lossless.
- **Hardware:** none (laptop / cloud container). Pi/Jetson are targets only.
- **Simulation:** custom kinematic simulator; control period 0.6 s; speeds 20–24 units/tick; drain 0.08 %/tick; charge 1.5 %/tick.
- **Metrics (measured):** −23.9 % mission time, −24.3 % makespan, −70.5 % waiting (100 seeds, 12 missions, 3 robots); median 20.5 %; 14/100 runs slower; 0 collisions (10-unit proximity monitor); A* 7 µs mean; tick 0.07 ms (3 robots) / 0.5 ms (10 robots); 10 robots: 150/150 tasks in 6000 ticks, 0 collisions, heavy congestion.
- **Limitations:** single process; no message delay/loss; no ROS 2/Zenoh; no Space-Time A*; no robot dynamics/localisation/footprints; self-defined baseline; single layout; no ML; not safety-rated; one mutex zone; demo guest permissions.
- **Novel contribution (honest):** an integration: shared-nothing agents + robot-run Contract-Net + soft-state leases + local deadlock resolution + dropout-recovery, evaluated against its own negotiation-disabled baseline. Individual techniques are standard (A*, Contract Net, leases, wait-for graphs). No claim of a new algorithm.
- **Repository terminology:** "peer mesh", "mesh conversation", "decentralised Contract-Net auction", "claims / leases", "soft-state", "stop-and-wait baseline", "mission time", "makespan".
- **Safe:** see T. **Unsafe:** see U.
