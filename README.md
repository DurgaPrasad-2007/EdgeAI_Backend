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

Open `http://localhost:3000`. The dashboard gets its fleet snapshot from `http://localhost:8000/api/fleet/state` and stays current over `ws://localhost:8000/ws/fleet`.

The controls are functional: sign in, run/pause the local twin, inject a blockage, create a warehouse move, observe its local cost bid and AMR assignment, and mark the persisted task complete. Users, tasks, and knowledge vectors are stored through a direct PostgreSQL connection and survive API restarts.

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

## What is implemented

- Three independent AMR agents exchange heartbeat, intent, and short-lived corridor lease messages.
- C-14 applies deterministic peer arbitration: safety buffer, task urgency, battery, then arrival time. Only its lease holder may enter, yielding zero simulated robot-to-robot collisions.
- A B-07 block triggers a local reroute and task handoff with no dashboard/cloud control dependency.
- Dashboard shows live position, battery, lease state, peer events, persisted task assignments, and the simulated 27% improvement against stop-and-wait (7:38 vs 10:30 for the same overlap scenario).

## Local edge API

`backend/app/main.py` owns the application lifespan and the explicit API surface. It is the simulation/telemetry bridge, not a motion-command server:

- `GET /health` and `GET /api/fleet/state`
- `POST /api/auth/token`, `GET /api/auth/me`, and admin-only `POST /api/users`
- `POST /api/fleet/simulation`, `POST /api/fleet/reset`, and `POST /api/fleet/blockages`
- `POST /api/fleet/intents`, `POST /api/fleet/reservations`, and `POST /api/tasks/bid`
- `GET /api/tasks`, `POST /api/tasks`, and `POST /api/tasks/{task_id}/complete`
- `WS /ws/fleet` for read-only live telemetry

The coordinator, database-backed login, JWT validation, roles, validation, headers, CORS, and rate limiting are covered in `backend/tests/test_api.py`. Run them with `cd backend && uv run pytest`. Database setup and migration instructions are in `docs/DATABASE.md`.

## Reference architecture selected

The design is informed by [Open-RMF `rmf_demos`](https://github.com/open-rmf/rmf_demos), the strongest production-ready open-source fleet-management reference found for heterogeneous robots, traffic deconfliction, task bidding, rerouting, and fleet telemetry.

Open-RMF itself maintains an authoritative schedule node, which conflicts with the problem statement's no-single-point-of-failure requirement. This implementation deliberately adopts only its proven patterns—fleet-adapter separation, intent/trajectory sharing, conflict-aware task costing, and telemetry—and replaces central dispatch/scheduling with peer-replicated leases and deterministic local arbitration. It is an adaptation, not a copy or an Open-RMF deployment.

## Production deployment boundary

This web application is the local fleet twin and presentation demo. For Raspberry Pi / Jetson deployment, run one ROS 2/DDS edge agent per AMR that owns the same `heartbeat → intent → lease → execute/replan` state machine, and feed the dashboard from a read-only local telemetry bridge. The dashboard must remain observational and never issue motion commands.
