from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("EDGEFLEET_DATA_PATH", str(tmp_path / "edgefleet-test.db"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def secure_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("EDGEFLEET_DATA_PATH", str(tmp_path / "edgefleet-secure-test.db"))
    monkeypatch.setenv("EDGEFLEET_ENV", "development")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-that-is-at-least-thirty-two-bytes-long")
    monkeypatch.setenv("JWT_ISSUER", "edgefleet-test-api")
    monkeypatch.setenv("JWT_AUDIENCE", "edgefleet-test-console")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Test-Admin-Password-2026!")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "20")
    with TestClient(app) as test_client:
        yield test_client


def access_token(*, audience: str = "edgefleet-test-console", expired: bool = False) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "11111111-1111-1111-1111-111111111111",
            "aud": audience,
            "iss": "edgefleet-test-api",
            "iat": now,
            "nbf": now,
            "exp": now - timedelta(minutes=1) if expired else now + timedelta(minutes=10),
            "jti": "test-token-id",
        },
        "test-only-secret-that-is-at-least-thirty-two-bytes-long",
        algorithm="HS256",
    )


def login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/api/auth/token", data={"username": email, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


def test_health_and_initial_fleet_state(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["fleet_size"] == 3
    assert health.json()["version"] == "0.2.0"
    assert "database" in health.json()

    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "alive"

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    response = client.get("/api/fleet/state")
    assert response.status_code == 200
    state = response.json()
    assert state["collision_count"] == 0
    assert len(state["robots"]) == 3


def test_blockage_is_generic_and_reservation_is_reported(client: TestClient) -> None:
    client.post("/api/fleet/reset")
    blocked = client.post("/api/fleet/blockages", json={"aisle_id": "B-07"})
    assert blocked.status_code == 200
    assert blocked.json()["blocked_nodes"] == ["AISLE-B07"]
    assert blocked.json()["aisle_blocked"] is True

    lease = client.post("/api/fleet/reservations", json={"robot_id": "AMR-02", "lease_seconds": 4.8})
    assert lease.status_code == 200
    assert lease.json()["reservation"] == "AMR-02"

    cleared = client.post("/api/fleet/blockages", json={"aisle_id": "B-07", "blocked": False})
    assert cleared.json()["blocked_nodes"] == []
    assert client.post("/api/fleet/blockages", json={"aisle_id": "NOWHERE-99"}).status_code == 422
    assert client.post("/api/fleet/reservations", json={"robot_id": "AMR-99"}).status_code == 422


def test_world_contract_and_snapshot_shape(client: TestClient) -> None:
    world = client.get("/api/world").json()
    assert {n["id"] for n in world["nodes"]} >= {"DOCK-W", "C-14", "AISLE-B07"}
    assert world["mutex_zones"] == ["C-14"]
    assert world["config"]["control_period_s"] == 0.6
    assert [r["id"] for r in world["robots"]] == ["AMR-01", "AMR-02", "AMR-03"]
    state = client.get("/api/fleet/state").json()
    assert {"seq", "tasks", "kpis", "p2p", "blocked_nodes"} <= set(state)


def test_websocket_receives_initial_telemetry(client: TestClient) -> None:
    with client.websocket_connect("/ws/fleet") as websocket:
        state = websocket.receive_json()
        assert state["robots"][0]["id"] == "AMR-01"


def test_task_creation_assignment_and_completion(client: TestClient) -> None:
    created = client.post("/api/tasks", json={"pickup": "RACK A-02", "destination": "Dock E", "priority": 86})
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == "Assigned"
    assert task["assigned_robot_id"] in {"AMR-01", "AMR-02", "AMR-03"}
    assert task["pickup"] == "RACK A-02" and task["destination"] == "DOCK-E"
    assert client.post("/api/tasks", json={"pickup": "P-08", "destination": "DOCK-E"}).status_code == 422

    listed = client.get("/api/tasks")
    assert any(item["id"] == task["id"] for item in listed.json())

    completed = client.post(f"/api/tasks/{task['id']}/complete")
    assert completed.status_code == 200
    assert completed.json()["status"] == "Completed"


def test_secure_api_rejects_missing_invalid_and_expired_tokens(secure_client: TestClient) -> None:
    assert secure_client.get("/api/fleet/state").status_code == 401
    assert secure_client.get("/api/fleet/state", headers={"Authorization": "Bearer malformed"}).status_code == 401
    assert secure_client.get("/api/fleet/state", headers={"Authorization": f"Bearer {access_token(expired=True)}"}).status_code == 401
    assert secure_client.get("/api/fleet/state", headers={"Authorization": f"Bearer {access_token(audience='wrong')}"}).status_code == 401


def test_audit_trail_records_actor_and_category(client: TestClient) -> None:
    created = client.post("/api/tasks", json={"pickup": "RACK A-02", "destination": "DOCK-E"})
    assert created.status_code == 201
    client.post("/api/fleet/blockages", json={"aisle_id": "B-07"})
    rows = client.get("/api/audit?limit=100").json()
    assert rows, "audit trail should not be empty"
    task_rows = [r for r in rows if r["category"] == "TASK" and created.json()["id"] in r["message"]]
    assert task_rows and all(r["actor"] == "local@edgefleet" for r in task_rows)
    control = [r for r in rows if r["category"] == "CONTROL" and "Obstacle reported" in r["message"]]
    assert control and control[0]["actor"] == "local@edgefleet"
    assert client.get("/api/audit?category=TASK").json() and all(r["category"] == "TASK" for r in client.get("/api/audit?category=TASK").json())


def test_secure_audit_records_logins_and_admin_changes(secure_client: TestClient) -> None:
    bad = secure_client.post("/api/auth/token", data={"username": "admin@example.test", "password": "wrong-password-123"})
    assert bad.status_code == 401
    token = login(secure_client, "admin@example.test", "Test-Admin-Password-2026!")
    headers = {"Authorization": f"Bearer {token}"}
    secure_client.post("/api/users", headers=headers, json={"email": "auditee@edgefleet.in", "password": "Test-Viewer-Password-2026!", "roles": ["viewer"]})
    rows = secure_client.get("/api/audit", headers=headers).json()
    messages = {(r["category"], r["actor"], r["message"]) for r in rows}
    assert ("AUTH", "admin@example.test", "login succeeded") in messages
    assert any(c == "AUTH" and m.startswith("login failed") for c, _, m in messages)
    assert any(c == "CONFIG" and a == "admin@example.test" and "auditee@edgefleet.in" in m for c, a, m in messages)


def test_secure_api_rejects_hardcoded_demo_tokens(secure_client: TestClient) -> None:
    for token in ("demo-jwt-token-sih26123", "demo-token", "mock_admin_token", "mock_operator_token_1"):
        headers = {"Authorization": f"Bearer {token}"}
        assert secure_client.get("/api/fleet/state", headers=headers).status_code == 401, token
        assert secure_client.post("/api/tasks", headers=headers, json={"pickup": "RACK A-01", "destination": "DOCK-E"}).status_code == 401
        with pytest.raises(Exception):
            with secure_client.websocket_connect("/ws/fleet", headers={"sec-websocket-protocol": f"edgefleet, {token}"}) as ws:
                ws.receive_json()


def test_secure_api_enforces_roles_and_response_headers(secure_client: TestClient) -> None:
    admin_token = login(secure_client, "admin@example.test", "Test-Admin-Password-2026!")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    created = secure_client.post("/api/users", headers=admin_headers, json={"email": "viewer@edgefleet.in", "password": "Test-Viewer-Password-2026!", "roles": ["viewer"]})
    assert created.status_code == 201, created.text
    new_user_id = created.json()["id"]

    # Test GET /api/users (admin allowed, viewer forbidden)
    admin_list = secure_client.get("/api/users", headers=admin_headers)
    assert admin_list.status_code == 200
    assert any(u["email"] == "viewer@edgefleet.in" for u in admin_list.json())

    viewer_headers = {"Authorization": f"Bearer {login(secure_client, 'viewer@edgefleet.in', 'Test-Viewer-Password-2026!')}"}
    viewer_list = secure_client.get("/api/users", headers=viewer_headers)
    assert viewer_list.status_code == 403

    # Test PATCH /api/users/{id}/status and DELETE /api/users/{id}
    status_patch = secure_client.patch(f"/api/users/{new_user_id}/status", headers=admin_headers, json={"active": False})
    assert status_patch.status_code == 200
    assert status_patch.json()["active"] is False

    del_resp = secure_client.delete(f"/api/users/{new_user_id}", headers=admin_headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True

    read = secure_client.get("/api/fleet/state", headers=admin_headers)
    assert read.status_code == 200
    assert read.headers["x-content-type-options"] == "nosniff"
    assert read.headers["x-frame-options"] == "DENY"
    assert read.headers["cache-control"] == "no-store"
    assert read.headers.get("x-request-id")
    assert secure_client.post("/api/tasks", headers=admin_headers, json={"pickup": "RACK A-01", "destination": "DOCK-E", "priority": 50}).status_code == 201


def test_secure_api_validates_input_bounds(secure_client: TestClient) -> None:
    operator_headers = {"Authorization": f"Bearer {login(secure_client, 'admin@example.test', 'Test-Admin-Password-2026!')}"}
    response = secure_client.post("/api/tasks", headers=operator_headers, json={"pickup": "", "destination": "D-01", "priority": 1000})
    assert response.status_code == 422


def test_secure_api_blocks_untrusted_cors_origin_and_rate_limits(secure_client: TestClient) -> None:
    token = login(secure_client, "admin@example.test", "Test-Admin-Password-2026!")
    headers = {"Authorization": f"Bearer {token}"}
    cors = secure_client.options(
        "/api/fleet/state",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
    )
    assert cors.status_code == 400
    assert "access-control-allow-origin" not in cors.headers
    statuses = [secure_client.get("/api/fleet/state", headers=headers).status_code for _ in range(25)]
    assert 429 in statuses
    assert statuses[0] == 200


def test_cache_manager_operations_and_rate_limit() -> None:
    import asyncio
    from app.cache import CacheManager

    async def _run() -> None:
        cache = CacheManager(default_ttl=60)
        await cache.connect()

        ping = await cache.ping()
        assert ping["mode"] == "in_memory"

        await cache.set_json("test:item", {"name": "Atlas", "battery": 90})
        val = await cache.get_json("test:item")
        assert val == {"name": "Atlas", "battery": 90}

        await cache.delete("test:item")
        assert await cache.get_json("test:item") is None

        # Test pattern deletion
        await cache.set_json("test:pattern:1", 1)
        await cache.set_json("test:pattern:2", 2)
        await cache.delete_pattern("test:pattern:*")
        assert await cache.get_json("test:pattern:1") is None
        assert await cache.get_json("test:pattern:2") is None

        # Test rate limiter
        allowed, remaining, retry_after = await cache.check_rate_limit("user1", limit=2, window_seconds=10)
        assert allowed and remaining == 1
        allowed, remaining, retry_after = await cache.check_rate_limit("user1", limit=2, window_seconds=10)
        assert allowed and remaining == 0
        allowed, remaining, retry_after = await cache.check_rate_limit("user1", limit=2, window_seconds=10)
        assert not allowed and remaining == 0 and retry_after > 0

        await cache.close()

    asyncio.run(_run())


def test_knowledge_vector_search(client: TestClient) -> None:
    # Add a knowledge chunk with 384-d vector
    dummy_vec = [0.05] * 384
    resp = client.post(
        "/api/knowledge/chunks",
        json={
            "source_name": "SOP-TEST-001",
            "content": "Corridor test arbitration protocol",
            "metadata": {"type": "test"},
            "embedding": dummy_vec,
        },
    )
    assert resp.status_code == 201
    chunk = resp.json()
    assert chunk["source_name"] == "SOP-TEST-001"

    # Search with embedding
    search_resp = client.post(
        "/api/knowledge/search",
        json={"embedding": dummy_vec, "limit": 2},
    )
    assert search_resp.status_code == 200
    results = search_resp.json()
    assert len(results) >= 1
    assert results[0]["source_name"] == "SOP-TEST-001"

    # Query with plain text
    query_resp = client.post(
        "/api/knowledge/query",
        json={"query": "arbitration protocol", "limit": 2},
    )
    assert query_resp.status_code == 200
    query_results = query_resp.json()
    assert len(query_results) >= 1

    q = {"query": "corridor C-14 emergency choke point yielding", "limit": 3}
    ranked = client.post("/api/knowledge/query", json=q).json()
    assert ranked[0]["source_name"] == "SOP-AMR-001"
    again = client.post("/api/knowledge/query", json=q).json()
    assert [r["source_name"] for r in again] == [r["source_name"] for r in ranked]
