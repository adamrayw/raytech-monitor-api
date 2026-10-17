import os
from unittest.mock import patch

from app.models.email_delivery import EmailDelivery
from app.models.notification_preference import NotificationPreference
from app.models.user import User


def test_get_notification_preferences_default(client, test_user):
    response = client.get("/settings/notifications")
    assert response.status_code == 200
    data = response.json()
    assert data["email_enabled"] is False
    assert data["http_down_enabled"] is True
    assert data["http_recovered_enabled"] is True
    assert data["heartbeat_missed_enabled"] is True
    assert data["ssl_expiring_enabled"] is True
    assert data["account_email"] == test_user.email


def test_patch_notification_preferences(client, test_user):
    payload = {
        "email_enabled": True,
        "http_down_enabled": True,
        "ssl_expiring_enabled": False,
    }
    response = client.patch("/settings/notifications", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["email_enabled"] is True
    assert data["http_down_enabled"] is True
    assert data["ssl_expiring_enabled"] is False

    # Verify persistent in next GET
    get_res = client.get("/settings/notifications")
    assert get_res.status_code == 200
    assert get_res.json()["email_enabled"] is True
    assert get_res.json()["ssl_expiring_enabled"] is False


def test_test_email_endpoint_disabled_globally(client, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    response = client.post("/settings/notifications/test-email")
    assert response.status_code == 400
    assert "globally disabled" in response.json()["detail"]


def test_test_email_endpoint_success(client, test_user, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("EMAIL_TEST_MODE", "true")
    monkeypatch.setenv("EMAIL_TEST_RECIPIENT", "admin-tester@raytech.cloud")

    response = client.post("/settings/notifications/test-email")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"
    assert data["recipient"] == "admin-tester@raytech.cloud"
    assert data["test_mode"] is True


def test_test_email_rate_limit(client, test_user, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("EMAIL_TEST_MODE", "true")
    monkeypatch.setenv("EMAIL_TEST_RECIPIENT", "admin-tester@raytech.cloud")

    # First request
    r1 = client.post("/settings/notifications/test-email")
    assert r1.status_code == 200

    # Second request immediately after -> 429
    r2 = client.post("/settings/notifications/test-email")
    assert r2.status_code == 429
    assert "wait 30 seconds" in r2.json()["detail"]


def test_email_history_isolation(client, db, test_user, other_user):
    # Deliveries for test_user
    d1 = EmailDelivery(
        user_id=test_user.id,
        recipient_email=test_user.email,
        event_type="incident_opened",
        subject="[Raytech Monitor] DOWN — API",
        status="sent",
    )
    # Delivery for other_user
    d2 = EmailDelivery(
        user_id=other_user.id,
        recipient_email=other_user.email,
        event_type="incident_opened",
        subject="[Raytech Monitor] DOWN — Secret DB",
        status="sent",
    )
    db.add_all([d1, d2])
    db.commit()

    # Request as test_user
    response = client.get("/settings/notifications/email-history")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["items"][0]["subject"] == "[Raytech Monitor] DOWN — API"
    assert data["items"][0]["recipient_email"] == test_user.email
    # other_user's delivery must not be present
    subjects = [item["subject"] for item in data["items"]]
    assert "[Raytech Monitor] DOWN — Secret DB" not in subjects
