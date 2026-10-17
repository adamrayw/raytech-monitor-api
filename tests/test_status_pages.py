from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.monitor_check import MonitorCheck
from app.models.status_page import StatusPage, StatusPageMonitor
from app.services.status_page_service import (
    calculate_overall_status,
    map_monitor_public_status,
    sanitize_incident_cause,
    validate_slug,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def user_monitors(db, test_user):
    m1 = Monitor(
        user_id=test_user.id,
        name="Production Web",
        url="https://web.raytech.cloud",
        type="http",
        interval_seconds=60,
        status="operational",
        is_active=True,
    )
    m2 = Monitor(
        user_id=test_user.id,
        name="Background Backup",
        url=None,
        type="heartbeat",
        interval_seconds=3600,
        status="healthy",
        is_active=True,
    )
    db.add_all([m1, m2])
    db.commit()
    db.refresh(m1)
    db.refresh(m2)
    return m1, m2


@pytest.fixture
def other_user_monitor(db, other_user):
    m = Monitor(
        user_id=other_user.id,
        name="Other User Private API",
        url="https://secret.other.cloud",
        type="http",
        interval_seconds=60,
        status="operational",
        is_active=True,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def test_slug_validation_rules():
    assert validate_slug("raytech") == "raytech"
    assert validate_slug("raytech-api-123") == "raytech-api-123"

    with pytest.raises(Exception):
        validate_slug("ab")  # Too short

    with pytest.raises(Exception):
        validate_slug("-leading-hyphen")

    with pytest.raises(Exception):
        validate_slug("trailing-hyphen-")

    with pytest.raises(Exception):
        validate_slug("has space")

    with pytest.raises(Exception):
        validate_slug("admin")  # Reserved


def test_create_draft_page(client, user_monitors):
    m1, _ = user_monitors
    payload = {
        "name": "Raytech Cloud Services",
        "slug": "raytech-draft",
        "description": "Public system status overview",
        "accent_color": "#00f0ff",
        "is_published": False,
        "show_incident_history": True,
        "monitors": [{"monitor_id": m1.id, "display_name": "Web Application", "display_order": 0}],
    }
    res = client.post("/status-pages", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "Raytech Cloud Services"
    assert data["slug"] == "raytech-draft"
    assert data["is_published"] is False
    assert len(data["monitors"]) == 1
    assert data["monitors"][0]["display_name"] == "Web Application"


def test_publish_valid_page_and_reject_zero_monitors(client):
    # Reject publish with 0 monitors
    empty_payload = {
        "name": "Empty Status",
        "slug": "empty-page",
        "is_published": True,
        "monitors": [],
    }
    r_empty = client.post("/status-pages", json=empty_payload)
    assert r_empty.status_code == 400
    assert "At least one monitor must be selected" in r_empty.json()["detail"]


def test_public_access_and_draft_protection(client, db, test_user, user_monitors):
    m1, _ = user_monitors
    page = StatusPage(
        user_id=test_user.id,
        name="Public Cloud",
        slug="public-cloud",
        is_published=False,  # Draft
    )
    db.add(page)
    db.commit()

    spm = StatusPageMonitor(status_page_id=page.id, monitor_id=m1.id)
    db.add(spm)
    db.commit()

    # Draft should return 404 publicly
    pub_res = client.get("/public/status/public-cloud")
    assert pub_res.status_code == 404

    # Publish page
    publish_res = client.post(f"/status-pages/{page.id}/publish")
    assert publish_res.status_code == 200
    assert publish_res.json()["is_published"] is True

    # Now accessible publicly without authentication!
    pub_res2 = client.get("/public/status/public-cloud")
    assert pub_res2.status_code == 200
    pub_data = pub_res2.json()
    assert pub_data["name"] == "Public Cloud"
    assert pub_data["slug"] == "public-cloud"
    assert pub_data["overall_status"] == "operational"
    assert len(pub_data["services"]) == 1
    assert pub_data["services"][0]["name"] == m1.name
    # Verify no private details leaked
    assert "user_id" not in pub_data
    assert "user" not in pub_data
    assert "url" not in pub_data["services"][0]


def test_unpublish_removes_public_access(client, db, test_user, user_monitors):
    m1, _ = user_monitors
    page = StatusPage(
        user_id=test_user.id,
        name="Toggle Page",
        slug="toggle-page",
        is_published=True,
    )
    db.add(page)
    db.commit()

    spm = StatusPageMonitor(status_page_id=page.id, monitor_id=m1.id)
    db.add(spm)
    db.commit()

    # Publicly accessible
    assert client.get("/public/status/toggle-page").status_code == 200

    # Unpublish
    unpub = client.post(f"/status-pages/{page.id}/unpublish")
    assert unpub.status_code == 200
    assert unpub.json()["is_published"] is False

    # Public access now 404
    assert client.get("/public/status/toggle-page").status_code == 404


def test_unique_slug_enforcement(client, db, test_user, other_user):
    p1 = StatusPage(user_id=other_user.id, name="Other", slug="duplicate-slug")
    db.add(p1)
    db.commit()

    res = client.post(
        "/status-pages",
        json={"name": "Mine", "slug": "duplicate-slug", "is_published": False, "monitors": []},
    )
    assert res.status_code == 400
    assert "already taken" in res.json()["detail"]


def test_cross_user_isolation_and_monitor_attachment(client, db, other_user, other_user_monitor, user_monitors):
    # 1. Cannot attach other user's monitor
    res = client.post(
        "/status-pages",
        json={
            "name": "Hacker Page",
            "slug": "hacker-page",
            "monitors": [{"monitor_id": other_user_monitor.id}],
        },
    )
    assert res.status_code == 400
    assert "Invalid or unauthorized monitor" in res.json()["detail"]

    # 2. Cannot access other user's status page
    other_page = StatusPage(user_id=other_user.id, name="Secret Page", slug="secret-page")
    db.add(other_page)
    db.commit()

    get_res = client.get(f"/status-pages/{other_page.id}")
    assert get_res.status_code == 404

    patch_res = client.patch(f"/status-pages/{other_page.id}", json={"name": "Hacked"})
    assert patch_res.status_code == 404

    del_res = client.delete(f"/status-pages/{other_page.id}")
    assert del_res.status_code == 404


def test_delete_page_preserves_monitors_and_incidents(client, db, test_user, user_monitors):
    m1, _ = user_monitors
    incident = Incident(
        monitor_id=m1.id,
        status="resolved",
        cause="HTTP 500 error",
        started_at=utcnow() - timedelta(hours=2),
        resolved_at=utcnow() - timedelta(hours=1),
    )
    db.add(incident)

    page = StatusPage(user_id=test_user.id, name="To Delete", slug="to-delete", is_published=False)
    db.add(page)
    db.commit()

    spm = StatusPageMonitor(status_page_id=page.id, monitor_id=m1.id)
    db.add(spm)
    db.commit()

    # Delete page
    del_res = client.delete(f"/status-pages/{page.id}")
    assert del_res.status_code == 204

    # Verify monitor and incident still exist!
    assert db.get(Monitor, m1.id) is not None
    assert db.get(Incident, incident.id) is not None
    assert db.get(StatusPage, page.id) is None


def test_status_mapping_and_overall_calculation():
    m_http_up = Monitor(type="http", status="operational", is_active=True)
    assert map_monitor_public_status(m_http_up) == "operational"

    m_http_down = Monitor(type="http", status="down", is_active=True)
    assert map_monitor_public_status(m_http_down) == "down"

    m_http_paused = Monitor(type="http", status="operational", is_active=False)
    assert map_monitor_public_status(m_http_paused) == "maintenance"

    m_hb_healthy = Monitor(type="heartbeat", status="healthy", is_active=True)
    assert map_monitor_public_status(m_hb_healthy) == "operational"

    m_hb_missed = Monitor(type="heartbeat", status="missed", is_active=True)
    assert map_monitor_public_status(m_hb_missed) == "down"

    # Overall statuses
    assert calculate_overall_status(["operational", "operational"])[0] == "operational"
    assert calculate_overall_status(["operational", "down"])[0] == "down"
    assert calculate_overall_status(["operational", "degraded"])[0] == "degraded"
    assert calculate_overall_status(["maintenance", "maintenance"])[0] == "maintenance"
    assert calculate_overall_status(["unknown"])[0] == "unknown"


def test_sanitized_public_incidents(client, db, test_user, user_monitors):
    m1, _ = user_monitors
    page = StatusPage(
        user_id=test_user.id,
        name="Incident Page",
        slug="incident-page",
        is_published=True,
        show_incident_history=True,
    )
    db.add(page)
    db.commit()

    spm = StatusPageMonitor(status_page_id=page.id, monitor_id=m1.id, display_name="Public Web")
    db.add(spm)

    # Sensitive internal cause with private IP and password
    inc = Incident(
        monitor_id=m1.id,
        status="resolved",
        cause="Connection refused to 10.0.0.45:5432 with password=secret",
        started_at=utcnow() - timedelta(minutes=45),
        resolved_at=utcnow() - timedelta(minutes=10),
        duration_seconds=2100,
    )
    db.add(inc)
    db.commit()

    res = client.get("/public/status/incident-page/incidents")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["service_name"] == "Public Web"
    assert "10.0.0.45" not in item["public_message"]
    assert "password" not in item["public_message"]
    assert item["public_message"] == "Service temporarily unavailable"


def test_public_history_uptime(client, db, test_user, user_monitors):
    m1, m2 = user_monitors
    page = StatusPage(
        user_id=test_user.id,
        name="Uptime Page",
        slug="uptime-page",
        is_published=True,
    )
    db.add(page)
    db.commit()

    db.add(StatusPageMonitor(status_page_id=page.id, monitor_id=m1.id, display_order=0))
    db.add(StatusPageMonitor(status_page_id=page.id, monitor_id=m2.id, display_order=1))

    # Add checks for m1
    today = utcnow()
    c1 = MonitorCheck(monitor_id=m1.id, checked_at=today, success=True, http_status=200, response_ms=120)
    c2 = MonitorCheck(monitor_id=m1.id, checked_at=today - timedelta(hours=1), success=True, http_status=200, response_ms=130)
    db.add_all([c1, c2])
    db.commit()

    res = client.get("/public/status/uptime-page/history?range=30d")
    assert res.status_code == 200
    data = res.json()
    assert len(data["services"]) == 2

    # HTTP service has 30 days history
    s_http = data["services"][0]
    assert s_http["service_type"] == "http"
    assert len(s_http["days"]) == 30
    today_point = s_http["days"][-1]
    assert today_point["has_data"] is True
    assert today_point["uptime_percent"] == 100.0
    assert today_point["status"] == "operational"

    # Heartbeat service indicates not applicable
    s_hb = data["services"][1]
    assert s_hb["service_type"] == "heartbeat"
    assert s_hb["note"] is not None
    assert all(d["has_data"] is False for d in s_hb["days"])
