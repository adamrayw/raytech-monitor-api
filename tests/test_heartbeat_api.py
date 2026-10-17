import pytest
from app.models.monitor import Monitor
from app.dependencies.auth import get_current_user
from app.main import app
from fastapi.testclient import TestClient


def test_create_heartbeat_monitor(client, db):
    """Test creating a heartbeat monitor returns a secret token and initial pending state."""
    response = client.post(
        "/monitors",
        json={
            "type": "heartbeat",
            "name": "Database Backup",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["type"] == "heartbeat"
    assert data["name"] == "Database Backup"
    assert data["status"] == "pending"
    assert data["url"] is None
    assert data["expected_interval_seconds"] == 3600
    assert data["grace_period_seconds"] == 600

    # Token returned on creation
    raw_token = data.get("heartbeat_token")
    assert raw_token is not None
    assert len(raw_token) >= 40  # 256-bit urlsafe token

    # Check database: token hash stored, not raw token
    monitor = db.query(Monitor).filter(Monitor.id == data["id"]).first()
    assert monitor.heartbeat_token_hash is not None
    assert monitor.heartbeat_token_hash != raw_token
    assert len(monitor.heartbeat_token_hash) == 64  # SHA-256


def test_heartbeat_token_not_exposed_in_details_or_list(client, db, test_user):
    """Normal GET monitor or list monitors endpoints must never expose secret token."""
    res = client.post(
        "/monitors",
        json={
            "type": "heartbeat",
            "name": "Cron Job",
            "expected_interval_seconds": 1800,
            "grace_period_seconds": 300,
        },
    )
    m_id = res.json()["id"]

    # GET /monitors/{id}
    detail_res = client.get(f"/monitors/{m_id}")
    assert detail_res.status_code == 200
    assert detail_res.json().get("heartbeat_token") is None

    # GET /monitors
    list_res = client.get("/monitors")
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    target = next(m for m in items if m["id"] == m_id)
    assert target.get("heartbeat_token") is None


def test_heartbeat_token_rotation(client, db):
    """Token regeneration issues a new token and immediately invalidates the old one."""
    res = client.post(
        "/monitors",
        json={
            "type": "heartbeat",
            "name": "Token Rotation Test",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    m_id = res.json()["id"]
    old_token = res.json()["heartbeat_token"]

    # Verify old token works
    ping1 = client.post(f"/heartbeat/{old_token}")
    assert ping1.status_code == 200

    # Regenerate token
    regen_res = client.post(f"/monitors/{m_id}/heartbeat/regenerate-token")
    assert regen_res.status_code == 200
    new_token = regen_res.json()["heartbeat_token"]
    assert new_token != old_token

    # Old token now rejected
    ping_old = client.post(f"/heartbeat/{old_token}")
    assert ping_old.status_code == 404

    # New token works
    ping_new = client.post(f"/heartbeat/{new_token}")
    assert ping_new.status_code == 200


def test_heartbeat_ownership_isolation(client, db, other_user):
    """Other users cannot regenerate tokens or view heartbeat events for unowned monitors."""
    res = client.post(
        "/monitors",
        json={
            "type": "heartbeat",
            "name": "Isolated Heartbeat",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    m_id = res.json()["id"]

    def override_other_user():
        return other_user

    app.dependency_overrides[get_current_user] = override_other_user
    with TestClient(app) as other_client:
        # Regenerate token unauthorized
        regen_res = other_client.post(f"/monitors/{m_id}/heartbeat/regenerate-token")
        assert regen_res.status_code == 404

        # Events unauthorized
        events_res = other_client.get(f"/monitors/{m_id}/heartbeat/events")
        assert events_res.status_code == 404


def test_heartbeat_pause_and_resume(client, db):
    """Pausing and resuming heartbeat monitors follows restart policy."""
    res = client.post(
        "/monitors",
        json={
            "type": "heartbeat",
            "name": "Pause Resume Job",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    m_id = res.json()["id"]
    token = res.json()["heartbeat_token"]

    # Ping to make it healthy
    client.post(f"/heartbeat/{token}")
    m = client.get(f"/monitors/{m_id}").json()
    assert m["status"] == "healthy"

    # Pause monitor
    pause_res = client.patch(f"/monitors/{m_id}", json={"is_active": False})
    assert pause_res.status_code == 200
    assert pause_res.json()["status"] == "paused"

    # Resume monitor: returns to pending waiting for fresh heartbeat
    resume_res = client.patch(f"/monitors/{m_id}", json={"is_active": True})
    assert resume_res.status_code == 200
    assert resume_res.json()["status"] == "pending"
    assert resume_res.json()["deadline_at"] is None


def test_heartbeat_events_history(client, db):
    """GET /monitors/{id}/heartbeat/events returns recent event history."""
    res = client.post(
        "/monitors",
        json={
            "type": "heartbeat",
            "name": "Events History Job",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    m_id = res.json()["id"]
    token = res.json()["heartbeat_token"]

    # Send 2 heartbeats
    client.post(f"/heartbeat/{token}")
    client.post(f"/heartbeat/{token}")

    events_res = client.get(f"/monitors/{m_id}/heartbeat/events")
    assert events_res.status_code == 200
    data = events_res.json()
    assert data["count"] >= 2
    assert all(e["monitor_id"] == m_id for e in data["items"])
