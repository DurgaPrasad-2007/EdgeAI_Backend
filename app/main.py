from __future__ import annotations

import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import Identity, TokenService, authenticate, bootstrap_admin, create_user, delete_user, list_users, require_roles, resolve_identity, update_user_status
from app.cache import CacheManager
from app.coordinator import FleetConflict, FleetCoordinator, FleetError
from app.database import Database
from app.graph import world_payload
from app.knowledge import WAREHOUSE_SOPS, text_to_embedding
from app.schemas import AccessToken, AgentFaultRequest, AuditRecord, BatteryFaultRequest, BlockageRequest, CurrentUser, FleetState, IntentRequest, KnowledgeChunk, KnowledgeChunkCreate, KnowledgeQuery, KnowledgeSearch, ReservationRequest, SimulationControl, TaskBidRequest, TaskCreate, TaskUpdate, TaskRecord, UserCreate, UserDetail, UserStatusUpdate
from app.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_environment()
    database = Database(settings.database_url)
    await database.initialize_test_schema()
    await database.check_connection()
    await bootstrap_admin(database, settings)
    cache = CacheManager()
    await cache.connect()
    coordinator = FleetCoordinator(database, cache=cache)
    await coordinator.start()
    await coordinator.seed_knowledge([KnowledgeChunkCreate(source_name=name, content=content, metadata=meta, embedding=text_to_embedding(content)) for name, content, meta in WAREHOUSE_SOPS])
    app.state.coordinator = coordinator
    app.state.settings = settings
    app.state.database = database
    app.state.cache = cache
    app.state.token_service = TokenService(settings)
    app.state.rate_windows = defaultdict(deque)
    try:
        yield
    finally:
        await coordinator.stop()
        await database.close()
        await cache.close()


app = FastAPI(title="EdgeFleet Local API", version="0.2.0", description="Local telemetry, task, and evidence API. It is never a robot motion controller.", lifespan=lifespan, docs_url="/docs", redoc_url=None)

@app.exception_handler(FleetConflict)
async def fleet_conflict_handler(_: Request, exc: FleetConflict) -> Response:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(FleetError)
async def fleet_error_handler(_: Request, exc: FleetError) -> Response:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# Browser origins are explicit, never reflected from the request.
def allowed_hosts() -> list[str]:
    configured_hosts = os.getenv("EDGEFLEET_ALLOWED_HOSTS", "")
    if not configured_hosts and os.getenv("EDGEFLEET_ENV", "development").strip().lower() != "production":
        configured_hosts = "localhost,127.0.0.1,testserver"
    hosts = [host.strip() for host in configured_hosts.split(",") if host.strip()]
    # Render supplies its public onrender.com hostname at runtime. Include only
    # that exact host; custom domains must remain explicitly configured.
    render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_hostname:
        hosts.append(render_hostname)
    return list(dict.fromkeys(hosts))


app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())
app.add_middleware(CORSMiddleware, allow_origins=[origin.strip() for origin in os.getenv("EDGEFLEET_ALLOWED_ORIGINS", "http://localhost:3000").split(",") if origin.strip()], allow_credentials=False, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS", "PUT"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"])


@app.middleware("http")
async def security_and_rate_limit(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    if request.url.path not in ("/health", "/health/live", "/health/ready"):
        configured_limit = request.app.state.settings.rate_limit_per_minute
        limit = min(10, configured_limit) if request.url.path == "/api/auth/token" else configured_limit
        key = f"{request.client.host if request.client else 'unknown'}:{request.url.path}"
        cache: CacheManager | None = getattr(request.app.state, "cache", None)

        if cache is not None:
            allowed, remaining, retry_after = await cache.check_rate_limit(key, limit, window_seconds=60)
            if not allowed:
                return Response(
                    status_code=429,
                    content='{"detail":"Rate limit exceeded"}',
                    media_type="application/json",
                    headers={"Retry-After": str(int(retry_after) or 60), "X-Request-ID": request_id}
                )
        else:
            windows: dict[str, deque[float]] = request.app.state.rate_windows
            now = time.monotonic()
            window = windows[key]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= limit:
                return Response(status_code=429, content='{"detail":"Rate limit exceeded"}', media_type="application/json", headers={"Retry-After": "60", "X-Request-ID": request_id})
            window.append(now)
    response = await call_next(request)
    response.headers.update({"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
    if request.app.state.settings.environment == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def get_coordinator(request: Request) -> FleetCoordinator:
    return request.app.state.coordinator


viewer = require_roles("viewer", "operator", "admin", "fleet-agent")
operator = require_roles("operator", "admin")
fleet_agent = require_roles("fleet-agent", "operator", "admin")


@app.post("/api/auth/token", response_model=AccessToken, tags=["authentication"])
async def issue_access_token(request: Request, form: OAuth2PasswordRequestForm = Depends()) -> AccessToken:
    user = await authenticate(request.app.state.database, form.username, form.password)
    coordinator: FleetCoordinator = request.app.state.coordinator
    if user is None:
        await coordinator.audit("AUTH", "login failed: invalid credentials", actor=form.username[:320])
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    settings: Settings = request.app.state.settings
    await coordinator.audit("AUTH", "login succeeded", actor=user.email)
    return AccessToken(access_token=request.app.state.token_service.issue(user), expires_in=settings.access_token_minutes * 60)


@app.get("/api/auth/me", response_model=CurrentUser, tags=["authentication"])
async def auth_me(identity: Identity = Depends(viewer)) -> CurrentUser:
    return CurrentUser(id=identity.subject, email=identity.email, roles=sorted(identity.roles))


@app.get("/api/users", response_model=list[UserDetail], tags=["authentication"])
async def get_users(request: Request, _: Identity = Depends(require_roles("admin"))) -> list[UserDetail]:
    users = await list_users(request.app.state.database)
    return [UserDetail(id=str(u.id), email=u.email, roles=u.roles, active=u.active, created_at=u.created_at) for u in users]


@app.post("/api/users", response_model=UserDetail, status_code=201, tags=["authentication"])
async def add_user(payload: UserCreate, request: Request, identity: Identity = Depends(require_roles("admin"))) -> UserDetail:
    user = await create_user(request.app.state.database, str(payload.email), payload.password, list(payload.roles))
    await request.app.state.coordinator.audit("CONFIG", f"user {user.email} created with roles {', '.join(user.roles)}", actor=identity.email)
    return UserDetail(id=str(user.id), email=user.email, roles=user.roles, active=user.active, created_at=user.created_at)


@app.patch("/api/users/{user_id}/status", response_model=UserDetail, tags=["authentication"])
async def set_user_status(user_id: str, payload: UserStatusUpdate, request: Request, identity: Identity = Depends(require_roles("admin"))) -> UserDetail:
    try:
        parsed_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")
    if str(parsed_id) == identity.subject and not payload.active:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own administrator account")
    user = await update_user_status(request.app.state.database, parsed_id, payload.active)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await request.app.state.coordinator.audit("CONFIG", f"user {user.email} {'activated' if user.active else 'deactivated'}", actor=identity.email)
    return UserDetail(id=str(user.id), email=user.email, roles=user.roles, active=user.active, created_at=user.created_at)


@app.delete("/api/users/{user_id}", tags=["authentication"])
async def remove_user(user_id: str, request: Request, identity: Identity = Depends(require_roles("admin"))) -> dict[str, object]:
    try:
        parsed_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")
    if str(parsed_id) == identity.subject:
        raise HTTPException(status_code=400, detail="Cannot delete your own administrator account")
    deleted = await delete_user(request.app.state.database, parsed_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    await request.app.state.coordinator.audit("CONFIG", f"user {user_id} deleted", actor=identity.email)
    return {"ok": True, "deleted_id": user_id}


@app.get("/health", tags=["system"])
async def health(request: Request, coordinator: FleetCoordinator = Depends(get_coordinator)) -> dict[str, object]:
    state = await coordinator.snapshot()
    settings: Settings = request.app.state.settings
    db_type = "postgresql" if settings.database_url.startswith("postgresql") else "sqlite"
    return {
        "status": "ok",
        "service": "edgefleet-api",
        "version": "0.2.0",
        "environment": settings.environment,
        "database": {"connected": True, "dialect": db_type},
        "fleet_size": len(state.robots),
        "simulation_running": state.running,
    }


@app.get("/health/live", tags=["system"])
async def health_live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready", tags=["system"])
async def health_ready(request: Request) -> dict[str, object]:
    try:
        await request.app.state.database.check_connection()
        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unready: {exc}")


@app.get("/api/audit", response_model=list[AuditRecord], tags=["audit"])
async def audit_trail(limit: int = 200, category: str | None = None, _: Identity = Depends(viewer), coordinator: FleetCoordinator = Depends(get_coordinator)) -> list[AuditRecord]:
    return await coordinator.list_audit(max(1, min(limit, 1000)), category)


@app.get("/api/world", tags=["fleet"])
async def world(_: Identity = Depends(viewer), coordinator: FleetCoordinator = Depends(get_coordinator)) -> dict[str, object]:
    return coordinator.world()


@app.get("/api/fleet/state", response_model=FleetState, tags=["fleet"])
async def fleet_state(_: Identity = Depends(viewer), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.snapshot()


@app.post("/api/fleet/simulation", response_model=FleetState, tags=["simulation"])
async def control_simulation(control: SimulationControl, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.set_simulation(control)


@app.post("/api/fleet/reset", response_model=FleetState, tags=["simulation"])
async def reset_simulation(_: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.reset()


@app.post("/api/fleet/intents", response_model=FleetState, tags=["peer protocol"])
async def publish_intent(intent: IntentRequest, _: Identity = Depends(fleet_agent), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.report_intent(intent)


@app.post("/api/fleet/reservations", response_model=FleetState, tags=["peer protocol"])
async def request_reservation(reservation: ReservationRequest, _: Identity = Depends(fleet_agent), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.request_reservation(reservation)


@app.post("/api/fleet/blockages", response_model=FleetState, tags=["fleet"])
async def report_blockage(blockage: BlockageRequest, _: Identity = Depends(fleet_agent), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.report_blockage(blockage)


@app.post("/api/fleet/faults/battery", response_model=FleetState, tags=["simulation"])
async def trigger_battery_fault(req: BatteryFaultRequest, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.set_robot_battery(req.robot_id, req.battery)


@app.post("/api/fleet/faults/dropout", response_model=FleetState, tags=["simulation"])
async def trigger_agent_dropout(req: AgentFaultRequest, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.simulate_agent_failure(req.robot_id)


@app.post("/api/tasks/bid", response_model=FleetState, tags=["tasks"])
async def bid_for_task(bid: TaskBidRequest, _: Identity = Depends(fleet_agent), coordinator: FleetCoordinator = Depends(get_coordinator)) -> FleetState:
    return await coordinator.bid_for_task(bid)


@app.get("/api/tasks", response_model=list[TaskRecord], tags=["tasks"])
async def list_tasks(_: Identity = Depends(viewer), coordinator: FleetCoordinator = Depends(get_coordinator)) -> list[TaskRecord]:
    return await coordinator.list_tasks()


@app.post("/api/tasks", response_model=TaskRecord, status_code=201, tags=["tasks"])
async def create_task(task: TaskCreate, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> TaskRecord:
    return await coordinator.create_task(task)


@app.post("/api/tasks/{task_id}/complete", response_model=TaskRecord, tags=["tasks"])
async def complete_task(task_id: str, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> TaskRecord:
    task = await coordinator.complete_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.patch("/api/tasks/{task_id}", response_model=TaskRecord, tags=["tasks"])
async def update_task(task_id: str, update: TaskUpdate, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> TaskRecord:
    task = await coordinator.update_task(task_id, update)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/api/tasks/{task_id}", status_code=204, tags=["tasks"])
async def delete_task(task_id: str, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)):
    success = await coordinator.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/knowledge/chunks", response_model=KnowledgeChunk, status_code=201, tags=["knowledge"])
async def add_knowledge(item: KnowledgeChunkCreate, _: Identity = Depends(operator), coordinator: FleetCoordinator = Depends(get_coordinator)) -> KnowledgeChunk:
    return await coordinator.add_knowledge(item)


@app.post("/api/knowledge/search", response_model=list[KnowledgeChunk], tags=["knowledge"])
async def search_knowledge(query: KnowledgeSearch, _: Identity = Depends(viewer), coordinator: FleetCoordinator = Depends(get_coordinator)) -> list[KnowledgeChunk]:
    return await coordinator.search_knowledge(query.embedding, query.limit)


@app.post("/api/knowledge/query", response_model=list[KnowledgeChunk], tags=["knowledge"])
async def query_knowledge(query: KnowledgeQuery, coordinator: FleetCoordinator = Depends(get_coordinator)) -> list[KnowledgeChunk]:
    emb = text_to_embedding(query.query)
    return await coordinator.search_knowledge(emb, query.limit)


@app.websocket("/ws/fleet")
async def fleet_telemetry(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings
    token = websocket.query_params.get("token")
    protocols = [value.strip() for value in websocket.headers.get("sec-websocket-protocol", "").split(",") if value.strip()]
    if not token and len(protocols) == 2 and protocols[0] == "edgefleet":
        token = protocols[1]

    subprotocol = "edgefleet" if "edgefleet" in protocols else None
    identity = None
    if token:
        try:
            identity = await resolve_identity(websocket.app.state.database, websocket.app.state.token_service, token)
        except Exception:
            pass

    # AUTH_REQUIRED applies to the live stream exactly as it does to REST: no valid identity, no telemetry.
    if settings.auth_required and identity is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept(subprotocol=subprotocol)
    coordinator: FleetCoordinator = websocket.app.state.coordinator
    queue = await coordinator.subscribe()
    try:
        await websocket.send_json((await coordinator.snapshot()).model_dump(mode="json"))
        while True:
            await websocket.send_json((await queue.get()).model_dump(mode="json"))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with suppress(RuntimeError):
            await coordinator.unsubscribe(queue)
