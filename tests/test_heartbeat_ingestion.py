import pytest
from datetime import datetime, timezone, timedelta
from app.models.monitor import Monitor
from app.models.incident import Incident
from app.models.notification import Notification
from app.models.heartbeat_event import HeartbeatEvent
from app.services.heartbeat_service import (
    generate_heartbeat_token,
    process_heartbeat,
    get_overdue_heartbeat_monitor_ids,
    evaluate_overdue_heartbeat,
)


def test_first_heartbeat_activation(db, test_user):
    """First heartbeat moves monitor from pending to healthy and sets deadlines."""
    raw_token, token_hash = generate_heartbeat_token()
    now = datetime.now(timezone.utc)

    monitor = Monitor(
        user_id=test_user.id,
        type="heartbeat",
        name="Backup Task",
        status="pending",
        is_active=True,
        expected_interval_seconds=3600,
        grace_period_seconds=600,
        heartbeat_token_hash=token_hash,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    success, msg, m = process_heartbeat(db, raw_token, client_ip="1.2.3.4")
    assert success is True
    assert m.status == "healthy"
    assert m.last_heartbeat_at is not None
    assert m.next_expected_at is not None
    assert m.deadline_at is not None

    # Verify deadline is expected + grace (~4200 seconds from now)
    deadline = m.deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    diff = (deadline - now).total_seconds()
    assert abs(diff - 4200) < 10

    # Verify event logged
    events = db.query(HeartbeatEvent).filter(HeartbeatEvent.monitor_id == monitor.id).all()
    assert len(events) == 1
    assert events[0].event_type == "ping"


def test_overdue_heartbeat_transitions_to_missed(db, test_user):
    """Worker detects deadline crossing, marks monitor missed, opens incident and notifies."""
    raw_token, token_hash = generate_heartbeat_token()
    now = datetime.now(timezone.utc)
    past_deadline = now - timedelta(minutes=5)

    monitor = Monitor(
        user_id=test_user.id,
        type="heartbeat",
        name="Hourly Sync",
        status="healthy",
        is_active=True,
        expected_interval_seconds=3600,
        grace_period_seconds=600,
        heartbeat_token_hash=token_hash,
        last_heartbeat_at=past_deadline - timedelta(hours=1),
        next_expected_at=past_deadline - timedelta(minutes=10),
        deadline_at=past_deadline,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    # Verify detected as overdue
    overdue_ids = get_overdue_heartbeat_monitor_ids(db)
    assert monitor.id in overdue_ids

    # Run evaluation
    res = evaluate_overdue_heartbeat(db, monitor.id)
    assert res is True
    db.refresh(monitor)
    assert monitor.status == "missed"

    # Verify incident opened
    incidents = db.query(Incident).filter(Incident.monitor_id == monitor.id).all()
    assert len(incidents) == 1
    assert incidents[0].status == "open"
    assert "Expected heartbeat was not received" in incidents[0].cause

    # Verify notification emitted
    notifs = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs) == 1
    assert notifs[0].event_type == "heartbeat_missed"
    assert "heartbeat missed" in notifs[0].title


def test_duplicate_missed_incident_and_notification_prevention(db, test_user):
    """Subsequent checks on an already missed monitor must not open duplicate incidents or spam notifications."""
    raw_token, token_hash = generate_heartbeat_token()
    past_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)

    monitor = Monitor(
        user_id=test_user.id,
        type="heartbeat",
        name="Dedup Test",
        status="healthy",
        is_active=True,
        expected_interval_seconds=3600,
        grace_period_seconds=600,
        heartbeat_token_hash=token_hash,
        deadline_at=past_deadline,
    )
    db.add(monitor)
    db.commit()

    # First run: becomes missed
    evaluate_overdue_heartbeat(db, monitor.id)
    db.commit()

    # Second run immediately afterward:
    # get_overdue_heartbeat_monitor_ids only queries status == 'healthy'
    assert monitor.id not in get_overdue_heartbeat_monitor_ids(db)

    # Calling evaluate_overdue_heartbeat directly does not re-open or re-notify
    res = evaluate_overdue_heartbeat(db, monitor.id)
    assert res is False
    assert db.query(Incident).filter(Incident.monitor_id == monitor.id).count() == 1
    assert db.query(Notification).filter(Notification.monitor_id == monitor.id).count() == 1


def test_recovery_from_missed(db, test_user):
    """When a heartbeat arrives for a missed monitor, incident is resolved and recovery notification is sent."""
    raw_token, token_hash = generate_heartbeat_token()
    now = datetime.now(timezone.utc)
    started_at = now - timedelta(hours=2)

    monitor = Monitor(
        user_id=test_user.id,
        type="heartbeat",
        name="Recovery Job",
        status="missed",
        is_active=True,
        expected_interval_seconds=3600,
        grace_period_seconds=600,
        heartbeat_token_hash=token_hash,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    # Seed open incident
    incident = Incident(
        monitor_id=monitor.id,
        status="open",
        cause="Expected heartbeat was not received.",
        started_at=started_at,
    )
    db.add(incident)
    db.commit()

    # Successful heartbeat arrives
    success, msg, m = process_heartbeat(db, raw_token)
    assert success is True
    assert m.status == "healthy"

    # Verify incident resolved
    db.refresh(incident)
    assert incident.status == "resolved"
    assert incident.resolved_at is not None
    assert incident.duration_seconds is not None and incident.duration_seconds > 0

    # Verify recovery notification emitted
    notifs = db.query(Notification).filter(
        Notification.monitor_id == monitor.id,
        Notification.event_type == "heartbeat_recovered",
    ).all()
    assert len(notifs) == 1
    assert "running again" in notifs[0].title


def test_invalid_heartbeat_token(db):
    """Unknown or invalid token returns 404 without revealing monitor existence."""
    success, msg, m = process_heartbeat(db, "completely_invalid_token_1234567890")
    assert success is False
    assert m is None
