import pytest
from datetime import datetime, timezone, timedelta
from app.models.monitor import Monitor
from app.models.ssl_check import SSLCheck
from app.dependencies.auth import get_current_user
from app.main import app
from fastapi.testclient import TestClient


def test_https_only_enforcement_create(client):
    """SSL monitoring cannot be enabled for HTTP URLs on creation."""
    # Attempting to create an HTTP monitor with SSL enabled
    response = client.post(
        "/monitors",
        json={
            "name": "HTTP Test",
            "url": "http://example.com/api",
            "interval_seconds": 60,
            "ssl_monitoring_enabled": True,
            "ssl_warning_days": 14,
        },
    )
    assert response.status_code == 422
    assert "HTTPS" in str(response.json())

    # Creating with HTTPS succeeds
    response = client.post(
        "/monitors",
        json={
            "name": "HTTPS Test",
            "url": "https://example.com/api",
            "interval_seconds": 60,
            "ssl_monitoring_enabled": True,
            "ssl_warning_days": 14,
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["ssl_monitoring_enabled"] is True
    assert data["ssl_status"] == "unknown"


def test_https_only_enforcement_update(client):
    """SSL monitoring cannot be enabled for HTTP URLs on update."""
    # Create valid HTTP monitor without SSL
    create_res = client.post(
        "/monitors",
        json={
            "name": "HTTP Monitor",
            "url": "http://example.com/api",
            "interval_seconds": 60,
            "ssl_monitoring_enabled": False,
        },
    )
    assert create_res.status_code == 201
    m_id = create_res.json()["id"]

    # Try to enable SSL while URL is still HTTP
    patch_res = client.patch(
        f"/monitors/{m_id}",
        json={"ssl_monitoring_enabled": True},
    )
    assert patch_res.status_code == 422
    assert "HTTPS" in patch_res.json().get("detail", "")


def test_first_check_scheduling(client, db):
    """Enabling SSL queues an immediate SSL check."""
    create_res = client.post(
        "/monitors",
        json={
            "name": "Schedule Test",
            "url": "https://secure.example.com",
            "interval_seconds": 60,
            "ssl_monitoring_enabled": False,
        },
    )
    assert create_res.status_code == 201
    m_id = create_res.json()["id"]

    # Enable SSL via PATCH
    patch_res = client.patch(
        f"/monitors/{m_id}",
        json={"ssl_monitoring_enabled": True, "ssl_warning_days": 30},
    )
    assert patch_res.status_code == 200

    # Query DB directly to check scheduling timestamp
    monitor = db.query(Monitor).filter(Monitor.id == m_id).first()
    assert monitor.ssl_monitoring_enabled is True
    assert monitor.ssl_status == "unknown"
    assert monitor.ssl_warning_days == 30
    assert monitor.ssl_next_check_at is not None
    # Next check should be queued immediately (within last or next few seconds)
    now = datetime.now(timezone.utc)
    # SQLite naive datetime normalization check
    sched = monitor.ssl_next_check_at
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=timezone.utc)
    assert abs((sched - now).total_seconds()) < 10


def test_paused_monitor_behavior(client, db):
    """Pausing a monitor sets SSL status to paused and clears next check."""
    create_res = client.post(
        "/monitors",
        json={
            "name": "Pause Test",
            "url": "https://secure.example.com",
            "interval_seconds": 60,
            "ssl_monitoring_enabled": True,
        },
    )
    assert create_res.status_code == 201
    m_id = create_res.json()["id"]

    # Pause monitor
    pause_res = client.patch(f"/monitors/{m_id}", json={"is_active": False})
    assert pause_res.status_code == 200

    monitor = db.query(Monitor).filter(Monitor.id == m_id).first()
    assert monitor.ssl_status == "paused"
    assert monitor.ssl_next_check_at is None

    # Resume monitor
    resume_res = client.patch(f"/monitors/{m_id}", json={"is_active": True})
    assert resume_res.status_code == 200

    db.refresh(monitor)
    assert monitor.ssl_status == "unknown"
    assert monitor.ssl_next_check_at is not None


def test_url_change_invalidates_certificate_state(client, db):
    """Changing monitor URL to a new host clears stale certificate data."""
    # Seed monitor with valid SSL metadata
    monitor = Monitor(
        user_id=1,
        name="Host Change Test",
        url="https://oldhost.com/test",
        interval_seconds=60,
        ssl_monitoring_enabled=True,
        ssl_status="valid",
        ssl_issuer="Old Host CA",
        ssl_subject="oldhost.com",
        ssl_expires_at=datetime.now(timezone.utc) + timedelta(days=90),
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    m_id = monitor.id

    # Update URL to new host
    patch_res = client.patch(
        f"/monitors/{m_id}",
        json={"url": "https://newhost.com/api"},
    )
    assert patch_res.status_code == 200

    db.refresh(monitor)
    assert monitor.ssl_status == "unknown"
    assert monitor.ssl_issuer is None
    assert monitor.ssl_subject is None
    assert monitor.ssl_expires_at is None
    assert monitor.ssl_next_check_at is not None


def test_ssl_ownership_isolation(client, db, other_user):
    """Users cannot access or query SSL details for monitors they do not own."""
    # Create monitor owned by test_user (id=1)
    create_res = client.post(
        "/monitors",
        json={
            "name": "Isolation Test",
            "url": "https://private.example.com",
            "ssl_monitoring_enabled": True,
        },
    )
    assert create_res.status_code == 201
    m_id = create_res.json()["id"]

    # Create a test client acting as other_user
    def override_other_user():
        return other_user

    app.dependency_overrides[get_current_user] = override_other_user
    with TestClient(app) as other_client:
        # GET /monitors/{id}/ssl should return 404
        ssl_res = other_client.get(f"/monitors/{m_id}/ssl")
        assert ssl_res.status_code == 404

        # GET /monitors/{id}/ssl/history should return 404
        hist_res = other_client.get(f"/monitors/{m_id}/ssl/history")
        assert hist_res.status_code == 404

        # PATCH /monitors/{id} should return 404
        patch_res = other_client.patch(
            f"/monitors/{m_id}",
            json={"ssl_warning_days": 7},
        )
        assert patch_res.status_code == 404


def test_ssl_detail_endpoint_contract(client, db, test_user):
    """GET /monitors/{id}/ssl returns structured SSL details matching spec."""
    expires = datetime.now(timezone.utc) + timedelta(days=45)
    monitor = Monitor(
        user_id=test_user.id,
        name="Contract Test",
        url="https://api.example.com/v1",
        ssl_monitoring_enabled=True,
        ssl_status="valid",
        ssl_issuer="Example CA",
        ssl_subject="api.example.com",
        ssl_expires_at=expires,
        ssl_valid_from=datetime.now(timezone.utc) - timedelta(days=45),
        ssl_last_checked_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ssl_last_successful_check_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    res = client.get(f"/monitors/{monitor.id}/ssl")
    assert res.status_code == 200
    data = res.json()
    assert data["monitor_id"] == monitor.id
    assert data["enabled"] is True
    assert data["status"] == "valid"
    assert data["hostname"] == "api.example.com"
    assert data["port"] == 443
    assert data["issuer"] == "Example CA"
    assert data["subject"] == "api.example.com"
    assert data["days_remaining"] in (44, 45)
    assert data["error_type"] is None


def test_existing_http_monitoring_remains_functional(client):
    """Standard HTTP monitors without SSL continue to work exactly as before."""
    res = client.post(
        "/monitors",
        json={
            "name": "Standard HTTP Service",
            "url": "http://legacy.internal/status",
            "interval_seconds": 300,
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["ssl_monitoring_enabled"] is False
    assert data["status"] == "unknown"
    assert data["interval_seconds"] == 300
    m_id = data["id"]

    # Verify list monitors includes it
    list_res = client.get("/monitors")
    assert list_res.status_code == 200
    monitors = list_res.json()["items"]
    assert any(m["id"] == m_id for m in monitors)
