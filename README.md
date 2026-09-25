# EdgeFleet — SIH26123

Local-first, full-stack multi-AMR coordination application for **SIH26123: Edge-AI Based Distributed Fleet Coordination for AMRs in Smart Warehouses**.

```
frontend/  Next.js operator dashboard
backend/   FastAPI edge API + SQLAlchemy ORM + Alembic migrations
```

The boundary is strict: `frontend/` contains presentation code and calls HTTP/WebSocket endpoints only. Database credentials, ORM models, migrations, password hashing, JWT signing/verification, authorization, and transactions exist only under `backend/`.

## Run locally

Terminal 1 (local edge API):

```bash
cd backend
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --env-file .env
```

Terminal 2 (dashboard):

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000` and sign in. The dashboard receives everything over `ws://localhost:8000/ws/fleet` (fleet, tasks, events, peer packets and KPIs in one snapshot) and loads the warehouse graph from `GET /api/world`. The frontend contains no simulation and no fallback data: if the backend is unreachable the UI says so and disables commands.

Tasks, the audit trail, users and knowledge vectors are stored in the database and survive restarts. Robots do not: after a restart every robot is back at its dock and any task that was in flight is re-queued and re-auctioned.

The FastAPI OpenAPI console is available at `http://localhost:8000/docs`.

## Run with Docker

From the backend directory, this command builds the image when needed and starts
the API:

```powershell
.\scripts\docker-up.ps1
```

Run it in the background with `-Detached`; force a clean dependency/image rebuild
with `-NoCache`. The equivalent direct Docker command is:

```powershell
docker compose up --build --remove-orphans
```

## CI/CD

GitHub Actions runs the locked dependency install, backend test suite, and a
Docker build for every pull request. A push to `main` or a `v*` tag additionally
publishes the image to GitHub Container Registry as `ghcr.io/<owner>/<repo>`.

The workflow uses the repository-scoped `GITHUB_TOKEN`; no registry secret is
needed. In repository settings, allow GitHub Actions workflows **Read and write
permissions** so the `packages: write` publish job can create or update the
package. Deploying the published image is intentionally left to the target
environment because this repository does not define a cloud host or deployment
credentials.

### Render

Create a **Docker** web service with this repository as its root directory and
leave Render's build and start command fields empty so it uses the Dockerfile.
Set the health-check path to `/health/live`. Render supplies `PORT`
automatically; the Gunicorn configuration uses it unless `BIND` is explicitly
set. Configure every value from `.env.production.example` in the Render
dashboard—especially `DATABASE_URL`, `JWT_SECRET`, `EDGEFLEET_ALLOWED_HOSTS`,
and `EDGEFLEET_ALLOWED_ORIGINS`. Do not upload `.env`.

Render's automatic `RENDER_EXTERNAL_HOSTNAME` is accepted by the backend. Add
any custom API domain explicitly to `EDGEFLEET_ALLOWED_HOSTS`. Production
startup fails immediately if its required database, secret, CORS, or host
configuration is absent.

## What is implemented

- **The world is data.** `app/graph.py` defines the warehouse graph, mutex zones and the fleet (`ROBOT_FLEET`). Adding a robot or a node there is all it takes: it appears in `/api/world`, in bidding and on every screen. A test guards against lanes that cross without a shared node.
- **Task lifecycle wired to motion.** `Queued → Assigned → In Progress → Completed` is driven by robots reaching their pickup and drop nodes, and every transition is persisted. Tasks no robot can carry, or that cannot be routed, are shown as `Blocked` rather than silently stuck.
- **Contract-Net allocation.** Each idle robot bids `(capacity margin × 0.05) + (battery × 0.4) − (A* distance × 0.08) + (priority × 0.25)`; queued tasks are re-auctioned every tick.
- **Collision avoidance.** A robot reserves its next two waypoints all-or-nothing before moving, so two robots can never share a node or meet head-on on a lane. Single-lane zones (C-14) additionally need a lease, arbitrated by `priority + 0.2 × (100 − battery)`. Robots waiting on each other are detected as a cycle and the lowest-ranked robot that can steps aside.
- **Real replanning.** Blocking any node (`POST /api/fleet/blockages`, `{"aisle_id": "...", "blocked": true|false}`) replans only the robots whose remaining route crosses it, with A*. A robot with no route waits as `Blocked` and retries.
- **Low battery / charging.** Idle robots below 30% go to the charge bay; robots below 20% do not bid.
- **Server-side KPIs and packets.** Utilisation, throughput, proximity violations and the peer-packet counter are computed by the coordinator from real events. `collision_count` is a measured proximity check, not a constant.
- **Persisted audit trail.** Every fleet event, login and admin change is stored with the acting user (`GET /api/audit`).
- **Knowledge search.** Deterministic hashed bag-of-words embeddings (stdlib only) with real cosine similarity; the SOPs are seeded on first start. It is lexical, not semantic.

Not implemented: the robots are simulated inside this process. There is no ROS 2/Zenoh transport, no D* Lite (A* is re-run on change), and no measured latency benchmark; those appear only in the project narrative.

## Local edge API

`backend/app/main.py` owns the application lifespan and the explicit API surface. It is the simulation/telemetry bridge, not a motion-command server:

- `GET /health`, `GET /api/world`, `GET /api/fleet/state`, `GET /api/audit`
- `POST /api/auth/token`, `GET /api/auth/me`, and admin-only `/api/users`
- `POST /api/fleet/simulation`, `POST /api/fleet/reset`, and `POST /api/fleet/blockages`
- `POST /api/fleet/intents`, `POST /api/fleet/reservations`, and `POST /api/tasks/bid`
- `GET|POST /api/tasks`, `PATCH|DELETE /api/tasks/{task_id}`, and `POST /api/tasks/{task_id}/complete`
- `POST /api/knowledge/query` (text) and `POST /api/knowledge/search` (vector)
- `WS /ws/fleet` for read-only live state; the token goes in the `Sec-WebSocket-Protocol` header (`edgefleet, <token>`), never the URL. `AUTH_REQUIRED` applies to the socket exactly as to REST.

Unknown locations or robots are rejected with `422`; requests that conflict with fleet state (for example reassigning to a busy robot) with `409`.

The coordinator, fleet behaviour, database-backed login, JWT validation, roles, headers, CORS and rate limiting are covered in `tests/`. Run them with `uv run pytest`. `tests/test_fleet.py` includes randomised workloads with blockages that assert every task completes with zero proximity violations. Database setup and migration instructions are in `docs/DATABASE.md`; run `alembic upgrade head` after pulling (revision `20260925_0002` adds the audit table and the task payload columns).

## Reference architecture selected

The design is informed by [Open-RMF `rmf_demos`](https://github.com/open-rmf/rmf_demos), the strongest production-ready open-source fleet-management reference found for heterogeneous robots, traffic deconfliction, task bidding, rerouting, and fleet telemetry.

Open-RMF itself maintains an authoritative schedule node, which conflicts with the problem statement's no-single-point-of-failure requirement. This implementation deliberately adopts only its proven patterns—fleet-adapter separation, intent/trajectory sharing, conflict-aware task costing, and telemetry—and replaces central dispatch/scheduling with peer-replicated leases and deterministic local arbitration. It is an adaptation, not a copy or an Open-RMF deployment.

## Production deployment boundary

This web application is the local fleet twin and presentation demo. For Raspberry Pi / Jetson deployment, run one ROS 2/DDS edge agent per AMR that owns the same `heartbeat → intent → lease → execute/replan` state machine, and feed the dashboard from a read-only local telemetry bridge. The dashboard must remain observational and never issue motion commands.
