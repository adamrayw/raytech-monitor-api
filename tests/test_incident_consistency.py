from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.email_delivery import EmailDelivery
from app.models.status_page import StatusPage, StatusPageMonitor
from app.services.http_checker import CheckResult
from app.services.monitor_runner import apply_check_result
from app.services.incident_service import (
    get_open_incident,
    handle_incident_transition,
    open_incident,
    resolve_incident,
)
from app.services.heartbeat_service import (
    evaluate_overdue_heartbeat,
    generate_heartbeat_token,
    process_heartbeat,
)
from app.services.status_page_service import calculate_overall_status
from scripts.reconcile_incidents import inspect_incidents, apply_reconciliation


def utcnow():
    return datetime.now(timezone.utc)


def test_http_lifecycle_down_recovery_and_incident_resolution(db, test_user):
    """
    Test HTTP lifecycle:
    Unknown -> Operational
    Operational -> 1st failure (remains operational) -> 2nd failure (Down + Incident Open)
    Down -> 1st success (remains Down, incident stays open) -> 2nd success (Operational + Incident Resolved)
    No duplicate incident.
    """
    monitor = Monitor(
        user_id=test_user.id,
        name="API Service",
        url="https://api.example.com",
        status="unknown",
        is_active=True,
    )
    db.add(monitor)
    db.commit()

    t0 = utcnow()

    # 1. Unknown -> First success -> Operational
    res_success = CheckResult(success=True, http_status=200, response_ms=120.0, error_type=None, error_message=None)
    prev = apply_check_result(db, monitor, res_success, t0)
    trans = handle_incident_transition(db, monitor, res_success, prev, t0)
    assert monitor.status == "operational"
    assert trans["event"] is None
    assert get_open_incident(db, monitor.id) is None

    # 2. Failure #1: Still operational (consecutive_failures = 1)
    t1 = t0 + timedelta(seconds=60)
    res_fail = CheckResult(success=False, http_status=500, response_ms=50.0, error_type="HTTP_STATUS_MISMATCH", error_message="HTTP 500 Internal Server Error")
    prev = apply_check_result(db, monitor, res_fail, t1)
    trans = handle_incident_transition(db, monitor, res_fail, prev, t1)
    assert monitor.status == "operational"
    assert monitor.consecutive_failures == 1
    assert trans["event"] is None

    # 3. Failure #2: Confirmed Down -> Incident Open
    t2 = t1 + timedelta(seconds=60)
    prev = apply_check_result(db, monitor, res_fail, t2)
    trans = handle_incident_transition(db, monitor, res_fail, prev, t2)
    assert monitor.status == "down"
    assert monitor.consecutive_failures == 2
    assert trans["event"] == "opened"
    inc = get_open_incident(db, monitor.id)
    assert inc is not None
    assert inc.status == "open"
    assert "500" in inc.cause

    # 4. Failure #3: Still Down, no duplicate incident
    t3 = t2 + timedelta(seconds=60)
    prev = apply_check_result(db, monitor, res_fail, t3)
    trans = handle_incident_transition(db, monitor, res_fail, prev, t3)
    assert monitor.status == "down"
    assert trans["event"] is None
    all_incs = list(db.scalars(select(Incident).where(Incident.monitor_id == monitor.id)).all())
    assert len(all_incs) == 1

    # 5. Recovery check #1: Remains Down (requires 2 consecutive successes)
    t4 = t3 + timedelta(seconds=60)
    prev = apply_check_result(db, monitor, res_success, t4)
    trans = handle_incident_transition(db, monitor, res_success, prev, t4)
    assert monitor.status == "down"
    assert monitor.consecutive_successes == 1
    assert trans["event"] is None
    assert get_open_incident(db, monitor.id) is not None

    # 6. Recovery check #2: Confirmed Operational -> Incident Resolved
    t5 = t4 + timedelta(seconds=60)
    prev = apply_check_result(db, monitor, res_success, t5)
    trans = handle_incident_transition(db, monitor, res_success, prev, t5)
    assert monitor.status == "operational"
    assert monitor.consecutive_successes == 2
    assert trans["event"] == "resolved"
    assert get_open_incident(db, monitor.id) is None

    db.refresh(inc)
    assert inc.status == "resolved"
    assert inc.resolved_at is not None
    assert inc.resolution_reason == "successful_check"
    assert inc.duration_seconds is not None and inc.duration_seconds >= 180


def test_http_recovery_to_degraded(db, test_user):
    """Down -> 2 successful checks with high latency -> Degraded -> Incident Resolved."""
    monitor = Monitor(
        user_id=test_user.id,
        name="Slow Service",
        url="https://slow.example.com",
        status="down",
        consecutive_failures=2,
        degraded_threshold_ms=500,
        is_active=True,
    )
    db.add(monitor)
    db.commit()

    t0 = utcnow()
    open_incident(
        db,
        monitor,
        CheckResult(success=False, http_status=503, response_ms=50.0, error_type="HTTP_503", error_message="Service Unavailable"),
        t0,
    )
    db.commit()
    assert get_open_incident(db, monitor.id) is not None

    slow_result = CheckResult(success=True, http_status=200, response_ms=1200.0, error_type=None, error_message=None)

    # 1st success -> still down
    t1 = t0 + timedelta(seconds=60)
    prev = apply_check_result(db, monitor, slow_result, t1)
    trans = handle_incident_transition(db, monitor, slow_result, prev, t1)
    assert monitor.status == "down"

    # 2nd success -> degraded & incident resolved
    t2 = t1 + timedelta(seconds=60)
    prev = apply_check_result(db, monitor, slow_result, t2)
    trans = handle_incident_transition(db, monitor, slow_result, prev, t2)
    assert monitor.status == "degraded"
    assert trans["event"] == "resolved"
    assert get_open_incident(db, monitor.id) is None


def test_configuration_change_administrative_closure(client, db, test_user):
    """
    When URL or expected status changes while an incident is Open:
    The incident must be administratively resolved (reason: configuration_changed),
    and NO false recovery notification or email is sent.
    """
    monitor = Monitor(
        user_id=test_user.id,
        name="Broken API",
        url="https://old.example.com/broken",
        status="down",
        is_active=True,
    )
    db.add(monitor)
    db.commit()

    inc = open_incident(
        db,
        monitor,
        CheckResult(success=False, http_status=404, response_ms=40.0, error_type="HTTP_404", error_message="HTTP 404 Not Found"),
        utcnow() - timedelta(minutes=15),
    )
    db.commit()
    assert inc.status == "open"

    # User edits URL via PATCH
    res = client.patch(
        f"/monitors/{monitor.id}",
        json={"url": "https://new.example.com/api"},
    )
    assert res.status_code == 200

    db.refresh(monitor)
    db.refresh(inc)

    # Monitor status is reset to unknown
    assert monitor.status == "unknown"
    assert monitor.url == "https://new.example.com/api"

    # Incident was administratively resolved with explanation
    assert inc.status == "resolved"
    assert inc.resolution_reason == "configuration_changed"
    assert inc.resolved_at is not None

    # Verify NO notification and NO email was generated
    notifs = db.scalars(select(Notification).where(Notification.incident_id == inc.id)).all()
    emails = db.scalars(select(EmailDelivery).where(EmailDelivery.user_id == test_user.id)).all()
    assert len(notifs) == 0
    assert len(emails) == 0


def test_pause_and_resume_lifecycle(client, db, test_user):
    """
    Pause when Down:
    - Incident stays open (pause is not a recovery, does NOT send false recovered email).
    - Monitor status becomes 'paused'.
    Resume after Pause:
    - Status becomes 'unknown' (does NOT immediately assume healthy).
    - Fresh check #1 proves recovery -> transitions to operational -> incident resolved with recovery email.
    """
    monitor = Monitor(
        user_id=test_user.id,
        name="Service To Pause",
        url="https://pause.example.com",
        status="down",
        is_active=True,
    )
    db.add(monitor)
    db.commit()

    inc = open_incident(
        db,
        monitor,
        CheckResult(success=False, http_status=500, response_ms=60.0, error_type="HTTP_500", error_message="HTTP 500"),
        utcnow() - timedelta(minutes=10),
    )
    db.commit()

    # 1. User Pauses monitor
    res = client.patch(f"/monitors/{monitor.id}", json={"is_active": False})
    assert res.status_code == 200
    db.refresh(monitor)
    db.refresh(inc)

    assert monitor.status == "paused"
    assert monitor.is_active is False
    # Incident is NOT marked resolved as a fake recovery
    assert inc.status == "open"

    # 2. User Resumes monitor
    res = client.patch(f"/monitors/{monitor.id}", json={"is_active": True})
    assert res.status_code == 200
    db.refresh(monitor)
    db.refresh(inc)

    assert monitor.is_active is True
    assert monitor.status == "unknown"
    # Not prematurely marked resolved before check
    assert inc.status == "open"

    # 3. Worker executes fresh check and succeeds
    t_now = utcnow()
    res_success = CheckResult(success=True, http_status=200, response_ms=80.0, error_type=None, error_message=None)
    prev = apply_check_result(db, monitor, res_success, t_now)
    trans = handle_incident_transition(db, monitor, res_success, prev, t_now)

    assert monitor.status == "operational"
    assert trans["event"] == "resolved"

    db.refresh(inc)
    assert inc.status == "resolved"
    assert inc.resolution_reason == "successful_check"


def test_heartbeat_lifecycle_missed_and_recovery(db, test_user):
    """
    Heartbeat monitor:
    Healthy -> Deadline passed -> Missed + Incident Open + heartbeat_missed notification.
    Missed -> Ping received -> Healthy + Incident Resolved + heartbeat_recovered notification.
    Duplicate ping -> No duplicate recovery notification.
    """
    raw_tok, tok_hash = generate_heartbeat_token()
    monitor = Monitor(
        user_id=test_user.id,
        type="heartbeat",
        name="Cron Job Worker",
        status="healthy",
        is_active=True,
        heartbeat_token_hash=tok_hash,
        expected_interval_seconds=300,
        grace_period_seconds=60,
        deadline_at=utcnow() - timedelta(seconds=10), # overdue!
    )
    db.add(monitor)
    db.commit()

    # Worker detects missed heartbeat
    evaluate_overdue_heartbeat(db, monitor.id)
    db.refresh(monitor)

    assert monitor.status == "missed"
    inc = get_open_incident(db, monitor.id)
    assert inc is not None
    assert inc.status == "open"
    assert "Expected heartbeat was not received" in inc.cause

    # Check notification emitted
    notifs = list(db.scalars(select(Notification).where(Notification.incident_id == inc.id)).all())
    assert len(notifs) == 1
    assert notifs[0].event_type == "heartbeat_missed"

    # Incoming ping proves recovery!
    ok, msg, mon = process_heartbeat(db, raw_tok)
    assert ok is True
    db.refresh(monitor)
    db.refresh(inc)

    assert monitor.status == "healthy"
    assert inc.status == "resolved"
    assert inc.resolution_reason == "heartbeat_received"

    # Verify recovery notification emitted exactly once
    rec_notifs = list(
        db.scalars(
            select(Notification).where(
                Notification.incident_id == inc.id,
                Notification.event_type == "heartbeat_recovered",
            )
        ).all()
    )
    assert len(rec_notifs) == 1

    # Second ping: no duplicate recovery notification
    ok2, msg2, mon2 = process_heartbeat(db, raw_tok)
    assert ok2 is True
    rec_notifs2 = list(
        db.scalars(
            select(Notification).where(
                Notification.incident_id == inc.id,
                Notification.event_type == "heartbeat_recovered",
            )
        ).all()
    )
    assert len(rec_notifs2) == 1


def test_monitor_delete_cascades_incidents(db, test_user):
    """Deleting a monitor safely removes its incidents without orphaned records."""
    monitor = Monitor(
        user_id=test_user.id,
        name="Temp Service",
        url="https://temp.example.com",
        status="operational",
        is_active=True,
    )
    db.add(monitor)
    db.commit()

    inc = Incident(
        monitor_id=monitor.id,
        status="resolved",
        cause="Temporary blip",
        started_at=utcnow() - timedelta(hours=2),
        resolved_at=utcnow() - timedelta(hours=1),
    )
    db.add(inc)
    db.commit()
    inc_id = inc.id

    db.delete(monitor)
    db.commit()

    assert db.get(Incident, inc_id) is None


def test_calculate_overall_status_permutations():
    """Verify Section 9 rules for public status page overall health."""
    # All operational
    st, hl = calculate_overall_status(["operational", "operational"])
    assert st == "operational"
    assert hl == "All Systems Operational"

    # One down
    st, hl = calculate_overall_status(["operational", "down"])
    assert st == "down"
    assert hl == "Service Disruption Detected"

    # One degraded
    st, hl = calculate_overall_status(["operational", "degraded"])
    assert st == "degraded"
    assert hl == "Degraded Performance"

    # Only maintenance / paused
    st, hl = calculate_overall_status(["maintenance"])
    assert st == "maintenance"
    assert hl == "Scheduled Maintenance in Progress"

    # Only unknown
    st, hl = calculate_overall_status(["unknown"])
    assert st == "unknown"
    assert hl == "Service Status Pending"

    # Maintenance + Unknown without operational: must NOT be operational!
    st, hl = calculate_overall_status(["maintenance", "unknown"])
    assert st == "maintenance"
    assert "Maintenance" in hl

    # Operational + Maintenance
    st, hl = calculate_overall_status(["operational", "maintenance"])
    assert st == "operational"
    assert "Some in Maintenance" in hl

    # Operational + Unknown
    st, hl = calculate_overall_status(["operational", "unknown"])
    assert st == "operational"
    assert "Some Status Pending" in hl


def test_public_status_page_consistency_and_history(client, db, test_user):
    """
    Test public status endpoints:
    - Service operational with resolved incident shows operational and shows history as RESOLVED.
    - Ongoing disruption shows down.
    """
    m1 = Monitor(user_id=test_user.id, name="Raytech Signal", url="https://signal.raytech.cloud", status="operational", is_active=True)
    m2 = Monitor(user_id=test_user.id, name="Raytech Signal API", url="https://api.signal.raytech.cloud", status="operational", is_active=True)
    db.add_all([m1, m2])
    db.commit()

    # Resolved incident for Raytech Signal API
    t_start = utcnow() - timedelta(hours=3)
    t_end = utcnow() - timedelta(hours=2)
    inc = Incident(
        monitor_id=m2.id,
        status="resolved",
        cause="HTTP 502 Bad Gateway",
        started_at=t_start,
        resolved_at=t_end,
        duration_seconds=3600,
        resolution_reason="successful_check",
    )
    db.add(inc)
    db.commit()

    page = StatusPage(
        user_id=test_user.id,
        name="Raytech Operational Status",
        slug="raytech-operational-test",
        is_published=True,
        show_incident_history=True,
    )
    db.add(page)
    db.commit()

    spm1 = StatusPageMonitor(status_page_id=page.id, monitor_id=m1.id, display_order=1)
    spm2 = StatusPageMonitor(status_page_id=page.id, monitor_id=m2.id, display_order=2)
    db.add_all([spm1, spm2])
    db.commit()

    # Query public status page
    res = client.get("/public/status/raytech-operational-test")
    assert res.status_code == 200
    data = res.json()
    assert data["overall_status"] == "operational"
    assert data["overall_headline"] == "All Systems Operational"
    assert len(data["services"]) == 2
    assert all(s["status"] == "operational" for s in data["services"])

    # Query public incidents
    inc_res = client.get("/public/status/raytech-operational-test/incidents")
    assert inc_res.status_code == 200
    inc_data = inc_res.json()
    assert inc_data["count"] == 1
    item = inc_data["items"][0]
    assert item["service_name"] == "Raytech Signal API"
    assert item["status"] == "resolved"
    assert "connectivity issue" in item["public_message"].lower() or "disruption" in item["public_message"].lower()


def test_reconciliation_script_dry_run_and_fix(db, test_user):
    """
    Test the reconciliation utility:
    1. A monitor is currently operational, but has an orphaned open incident (the Raytech Signal API scenario).
    2. Dry run identifies it with exact cause without modifying DB.
    3. Fix mode resolves it safely with resolution_reason='reconciled_stale_incident', no emails sent.
    """
    mon = Monitor(
        user_id=test_user.id,
        name="Raytech Signal API Production",
        url="https://api.signal.raytech.cloud",
        status="operational",
        is_active=True,
        last_success_at=utcnow(),
    )
    db.add(mon)
    db.commit()

    # Orphaned open incident
    stale_inc = Incident(
        monitor_id=mon.id,
        status="open",
        cause="HTTP 502 Bad Gateway",
        started_at=utcnow() - timedelta(days=2),
    )
    db.add(stale_inc)
    db.commit()

    # 1. Run inspect_incidents
    records = inspect_incidents(db)
    assert len(records) == 1
    rec = records[0]
    assert rec["incident_id"] == stale_inc.id
    assert rec["is_inconsistent"] is True
    assert rec["proposed_action"] == "RESOLVE_STALE"
    assert "reconciled_stale_incident" in rec["resolution_reason"]

    # Verify dry run does not modify DB
    db.refresh(stale_inc)
    assert stale_inc.status == "open"

    # 2. Run apply_reconciliation
    resolved_count = apply_reconciliation(db, records)
    assert resolved_count == 1

    db.refresh(stale_inc)
    assert stale_inc.status == "resolved"
    assert stale_inc.resolution_reason == "reconciled_stale_incident"
    assert stale_inc.resolved_at is not None

    # Verify zero notifications and zero emails were created
    notifs = db.scalars(select(Notification).where(Notification.incident_id == stale_inc.id)).all()
    emails = db.scalars(select(EmailDelivery).where(EmailDelivery.user_id == test_user.id)).all()
    assert len(notifs) == 0
    assert len(emails) == 0
