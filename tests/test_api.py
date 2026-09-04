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


def test_blockage_reassigns_task_and_reservation_is_reported(client: TestClient) -> None:
    client.post("/api/fleet/reset")
    blocked = client.post("/api/fleet/blockages", json={"aisle_id": "B-07"})
    assert blocked.status_code == 200
    robots = {robot["id"]: robot for robot in blocked.json()["robots"]}
    assert robots["AMR-03"]["status"] == "Rerouting"
    assert "P-23" in robots["AMR-01"]["task"]

    lease = client.post("/api/fleet/reservations", json={"robot_id": "AMR-02", "lease_seconds": 4.8})
    assert lease.status_code == 200
    assert lease.json()["reservation"] == "AMR-02"


def test_websocket_receives_initial_telemetry(client: TestClient) -> None:
    with client.websocket_connect("/ws/fleet") as websocket:
        state = websocket.receive_json()
        assert state["robots"][0]["id"] == "AMR-01"


def test_task_creation_assignment_and_completion(client: TestClient) -> None:
    created = client.post("/api/tasks", json={"pickup": "P-08", "destination": "Dock E", "priority": 86})
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == "Assigned"
    assert task["assigned_robot_id"] in {"AMR-01", "AMR-02", "AMR-03"}

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
    assert secure_client.post("/api/tasks", headers=admin_headers, json={"pickup": "P-01", "destination": "D-01", "priority": 50}).status_code == 201


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
