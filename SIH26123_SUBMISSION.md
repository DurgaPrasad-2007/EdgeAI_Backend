# SIH26123 — EdgeFleet

> PPT-ready research citations, implementation references, attribution text, and compact slide copy are available in [`SIH_PPT_REFERENCES.md`](SIH_PPT_REFERENCES.md).

**Problem statement:** SIH26123, *Edge-AI Based Distributed Fleet Coordination for Autonomous Mobile Robots (AMRs) in Smart Warehouses*  
**Organisation / theme:** Bharat Electronics Limited / Smart Automation  
**Submission position:** software-first, local-edge architecture for at least three AMRs. This document is an accurate technical disclosure, not a claim of certified autonomous operation.

## One-line solution

EdgeFleet lets every AMR keep a local copy of nearby peers’ position and intended corridor occupancy, negotiate renewable short leases at choke points, and re-bid work when an aisle closes. The web console observes and authorizes operations; it is never a central motion controller.

## Why it addresses the problem

| SIH requirement | Implemented design |
| --- | --- |
| Decentralized communication | An intent/lease protocol carries robot ID, corridor, ETA, priority and expiry. The prototype exposes these peer messages; deployment maps them to local ROS 2 DDS or a secured LAN transport. |
| Collision and deadlock avoidance | A short-lived lease prevents simultaneous entry to a conflict cell. Deterministic scoring (safety buffer → urgency → battery → arrival) breaks ties; expiry and a yield path prevent permanent ownership. |
| Blockage, reroute and handoff | A blockage event invalidates the affected aisle; the affected robot takes a local detour and the task is re-bid among eligible robots. |
| Edge hardware | The coordination state machine is small, asynchronous Python and can run per robot on Raspberry Pi 5 / Jetson-class devices. PostgreSQL is audit/retrieval storage, not a safety dependency. |
| Dashboard | Separate Next.js console shows real-time fleet state, battery, leases, collision count, event log, live task creation and completion. |

## Architecture

```text
 AMR-01 edge agent  ←→  AMR-02 edge agent  ←→  AMR-03 edge agent
   local pose / intent        direct LAN peer messages       local pose / intent
          │                           │                            │
          └──────── local dashboard bridge / FastAPI ──────────────┘
                                      │ asynchronous audit only
                       Direct PostgreSQL + pgvector + Alembic
                                      │
                        Next.js operator console (OAuth 2.0)
```

### Safety boundary

1. Each robot independently applies a conservative stop/yield rule if a peer heartbeat, pose, or lease is stale.
2. The database, dashboard, cloud link, and task allocator may fail without granting passage or issuing wheel commands.
3. A real AMR integration must retain OEM emergency stop, bumper/LiDAR protective fields, speed limits, and functional-safety validation. EdgeFleet is not an E-stop replacement or a safety-certified controller.

## Algorithms and implementation decisions

- **Reservation-first coordination:** peers publish an intent before entering a narrow corridor. The highest deterministic score receives a bounded lease. Every lease expires, so failure does not make a corridor permanently unavailable.
- **Deadlock recovery:** a loser yields at a holding point, waits for release/expiry, then retries with a monotonic tie-breaker. A detected blockage triggers a lane exclusion and bounded detour search. Persistent uncertainty results in a safe stop, not optimistic motion.
- **Task allocation:** eligible AMRs bid from travel cost, task urgency and battery margin; the highest utility receives the work. A blockage or low battery triggers rebid.
- **Vector evidence store:** pgvector indexes 384-dimensional embeddings of approved SOPs, aisle incident reports and maintenance instructions. Retrieval is separate from collision control and is audited.
- **Reference architecture:** Open-RMF’s fleet-adapter pattern informed the clear separation of robot integration, traffic data, task handling, and operator UI. We deliberately do not reuse its central schedule node as the safety decision-maker because SIH asks for decentralized resilience.

## Research takeaways used

1. Open-RMF models fleet integration through adapters and traffic negotiation; that supports a clean robot-specific adapter boundary. The project uses this pattern, while moving real-time passage decisions to robot peers. [Open-RMF fleet adapter](https://github.com/open-rmf/rmf_ros2/blob/main/rmf_fleet_adapter_python/README.md)
2. ORCA shows that reciprocal local velocity constraints can efficiently avoid collisions among independent agents. Our production integration treats its local obstacle/safety envelope as the lower-level guard beneath corridor lease negotiation. [van den Berg et al., *Reciprocal n-body Collision Avoidance*](https://gamma.cs.unc.edu/ORCA/publications/ORCA.pdf)
3. MAPF research distinguishes vertex and edge conflicts and makes baselines comparable. Our evaluation therefore records collision count, conflict-cell entry, makespan, latency, and the same task set for both policies. [Stern et al., *Multi-Agent Pathfinding: Definitions, Variants, and Benchmarks*](https://arxiv.org/abs/1906.08291)
4. DC-MRTA demonstrates the relevance of decentralized allocation/navigation to complex warehouse settings. We take the practical design lesson—allocate and navigate jointly under change—without representing its RL results as ours. [DC-MRTA paper](https://arxiv.org/abs/2209.02865)

Fictional novels are not used as technical evidence. The submission uses primary papers, open-source implementation references, and official platform documentation instead.

## Measurable success plan

| Metric | Baseline | EdgeFleet target | Measurement |
| --- | --- | --- | --- |
| Inter-robot collisions | stop-and-wait run | 0 | geometric footprint overlap + safety sensor logs |
| Completion time | same fixed overlapping-path workload | ≥20% reduction | makespan across at least 30 seeded runs |
| Conflict decision latency | N/A | p95 < 150 ms on local LAN | monotonic timestamps at peer receipt/grant |
| Deadlock recovery | manual / timeout | automatic bounded retry or safe stop | injected choke-point and packet-loss tests |
| Network resilience | central-server loss | local safety continues | isolate dashboard/database during run |

The current UI shows a simulated comparison. The percentage must be replaced with the recorded median and confidence interval from the stated test harness before final SIH claims are made.

## Security, privacy, and compliance disclosure

- The local operator directory uses Argon2 password hashes and short-lived signed JWTs. FastAPI validates issuer, audience, expiry, not-before, token ID, and subject, then reloads active roles from PostgreSQL.
- Role-based access: `viewer` reads; `operator`/`admin` create tasks and control simulations; `fleet-agent` publishes robot intent, leases, and blockages.
- The browser has no database credentials or direct database access. Database URLs and signing keys stay server-side and are never committed.
- Exact-origin CORS, rate limiting, secure response headers, correlation IDs, validation limits, and no-store API responses are enabled in the API.
- Data minimization: retain only operational telemetry, task records, and approved operational documents. Define retention, access review, backup encryption, incident response, and deletion procedures with the warehouse owner before deployment.
- Before deployment to a live site, complete the organisation’s privacy, cybersecurity, electrical/robot safety, RF/Wi-Fi, and applicable Indian legal/regulatory review. This project does **not** claim ISO 3691-4, IEC 61508, ISO 27001, DPDP Act, or any other certification/compliance attestation.

## Open-source and IP disclosure

- Architectural reference: Open-RMF, Apache-2.0. No Open-RMF source code is copied into this repository.
- Runtime stack: Next.js/React, FastAPI, SQLAlchemy, Alembic, PostgreSQL, pgvector. Each has its own upstream licence; produce an SBOM and license scan for the final release.
- Novel source code, topology, UI, and SIH documentation in this workspace are prepared for this solution. Confirm team ownership, third-party asset licences, and institute submission rules before submission.

## Demo flow for judges

1. Apply the checked-in Alembic migration to PostgreSQL, then start API and frontend using `docs/DATABASE.md`.
2. Run the three-AMR simulation and observe a conflict-cell lease.
3. Inject B-07 blockage; verify reroute and task handoff in the event stream.
4. Create a task; verify a locally computed winner and persistent task record.
5. Pause the dashboard/database connection; explain that peer safety logic and OEM robot safety layers remain local.
6. Show the controlled benchmark using the identical workload and its recorded logs.

## Known limits and next engineering milestones

- This repository is a functional digital twin/API, not a hardware-certified control stack.
- Replace simulated pose updates with signed ROS 2/DDS peer messages, authenticated robot identities, time synchronization, and real map/footprint data.
- Add automated packet-loss, partition, stale-pose, obstacle, battery, and adversarial-auth tests; then run repeated physical trials.
- Establish the identity-provider tenant, production TLS/reverse proxy, backup/restore drill, vulnerability scanning, SBOM, change control, and safety case before operational launch.
