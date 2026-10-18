import os
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app

from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.status_page import StatusPage
from app.models.subscription import UserSubscription
from app.models.user import User
from app.services.email_service import enqueue_email_for_notification
from app.services.subscription_service import (
    PLAN_CONFIG,
    get_effective_plan,
    get_entitlements,
    get_or_create_subscription,
    get_subscription_usage,
    switch_user_plan_dev,
)


def test_new_user_gets_free_subscription(db, other_user, client):
    """1. New user gets Free plan by default and missing subscription does not grant Pro."""
    from app.dependencies.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: other_user

    res = client.get("/subscription")
    assert res.status_code == 200
    data = res.json()
    assert data["plan"] == "free"
    assert data["entitlements"]["max_http_monitors"] == 3
    assert data["entitlements"]["min_http_interval_seconds"] == 300
    assert data["entitlements"]["ssl_monitoring"] is False
    assert data["entitlements"]["email_alerts"] is False


def test_missing_subscription_defaults_to_free(db):
    """2. If subscription record is absent, it defaults to Free plan and never grants Pro."""
    plan_id, sub = get_effective_plan(db, 999999)
    assert plan_id == "free"
    entitlements = get_entitlements(db, 999999)
    assert entitlements["max_http_monitors"] == 3
    assert entitlements["ssl_monitoring"] is False


def test_pro_entitlements_work(db, test_user, client):
    """3. Pro entitlements grant higher quotas and premium capabilities."""
    switch_user_plan_dev(db, test_user.id, "pro")

    res = client.get("/subscription")
    assert res.status_code == 200
    data = res.json()
    assert data["plan"] == "pro"
    assert data["entitlements"]["max_http_monitors"] == 20
    assert data["entitlements"]["min_http_interval_seconds"] == 60
    assert data["entitlements"]["max_heartbeat_monitors"] == 20
    assert data["entitlements"]["max_status_pages"] == 5
    assert data["entitlements"]["ssl_monitoring"] is True
    assert data["entitlements"]["email_alerts"] is True


def test_invalid_plan_rejected(db, test_user, client):
    """4. Switching to an invalid plan is rejected."""
    res = client.post("/subscription/admin/switch-plan", json={"plan": "enterprise_gold"})
    assert res.status_code == 400


def test_dev_switch_disabled_in_production(db, test_user, client, monkeypatch):
    """5 & 6. Dev-only plan switching is strictly disabled when ENVIRONMENT is production."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    res = client.post("/subscription/admin/switch-plan", json={"plan": "pro"})
    assert res.status_code == 403
    assert "disabled in production" in res.json()["detail"]


def test_free_creates_up_to_3_http_monitors(db, test_user, client):
    """7 & 8. Free user can create up to 3 HTTP monitors; 4th creation is blocked."""
    switch_user_plan_dev(db, test_user.id, "free")

    for i in range(1, 4):
        res = client.post(
            "/monitors",
            json={
                "name": f"HTTP Monitor {i}",
                "type": "http",
                "url": f"https://example.com/{i}",
                "interval_seconds": 300,
            },
        )
        assert res.status_code == 201, f"Failed at monitor {i}: {res.text}"

    # 4th should fail with 403 PLAN_LIMIT_EXCEEDED
    res4 = client.post(
        "/monitors",
        json={
            "name": "HTTP Monitor 4",
            "type": "http",
            "url": "https://example.com/4",
            "interval_seconds": 300,
        },
    )
    assert res4.status_code == 403
    err = res4.json()["detail"]
    assert err["code"] == "PLAN_LIMIT_EXCEEDED"
    assert err["resource"] == "http_monitors"
    assert err["limit"] == 3


def test_pro_creates_up_to_20_http_monitors(db, test_user, client):
    """9 & 10. Pro user can create up to 20 HTTP monitors; 21st creation is blocked."""
    switch_user_plan_dev(db, test_user.id, "pro")

    for i in range(1, 21):
        m = Monitor(
            user_id=test_user.id,
            type="http",
            name=f"Pro HTTP {i}",
            url=f"https://pro.example.com/{i}",
            interval_seconds=60,
        )
        db.add(m)
    db.commit()

    # 21st creation should fail
    res = client.post(
        "/monitors",
        json={
            "name": "Pro HTTP 21",
            "type": "http",
            "url": "https://pro.example.com/21",
            "interval_seconds": 60,
        },
    )
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert res.json()["detail"]["limit"] == 20


def test_free_cannot_choose_60s_interval(db, test_user, client):
    """11 & 12. Free user cannot choose 60-second check interval; Pro can."""
    switch_user_plan_dev(db, test_user.id, "free")

    res = client.post(
        "/monitors",
        json={
            "name": "Fast Monitor",
            "type": "http",
            "url": "https://fast.example.com",
            "interval_seconds": 60,
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert res.json()["detail"]["min_interval"] == 300

    # Switch to Pro
    switch_user_plan_dev(db, test_user.id, "pro")
    res_pro = client.post(
        "/monitors",
        json={
            "name": "Fast Monitor Pro",
            "type": "http",
            "url": "https://fast.example.com",
            "interval_seconds": 60,
        },
    )
    assert res_pro.status_code == 201
    assert res_pro.json()["interval_seconds"] == 60


def test_free_cannot_update_to_60s_interval(db, test_user, client):
    """Updating HTTP monitor to interval below plan minimum is blocked."""
    switch_user_plan_dev(db, test_user.id, "free")

    m = Monitor(
        user_id=test_user.id,
        type="http",
        name="Update Test",
        url="https://example.com/update",
        interval_seconds=300,
    )
    db.add(m)
    db.commit()
    db.refresh(m)

    res = client.patch(f"/monitors/{m.id}", json={"interval_seconds": 60})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "PLAN_LIMIT_EXCEEDED"


def test_heartbeat_limits(db, test_user, client):
    """14, 15, 16. Free allows 1 heartbeat monitor; 2nd rejected; Pro allows up to 20."""
    switch_user_plan_dev(db, test_user.id, "free")

    res1 = client.post(
        "/monitors",
        json={
            "name": "HB 1",
            "type": "heartbeat",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    assert res1.status_code == 201

    res2 = client.post(
        "/monitors",
        json={
            "name": "HB 2",
            "type": "heartbeat",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    assert res2.status_code == 403
    assert res2.json()["detail"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert res2.json()["detail"]["resource"] == "heartbeat_monitors"

    # Switch to Pro
    switch_user_plan_dev(db, test_user.id, "pro")
    res2_pro = client.post(
        "/monitors",
        json={
            "name": "HB 2",
            "type": "heartbeat",
            "expected_interval_seconds": 3600,
            "grace_period_seconds": 600,
        },
    )
    assert res2_pro.status_code == 201


def test_ssl_monitoring_entitlement(db, test_user, client):
    """17 & 18. Free cannot newly enable SSL monitoring; Pro can."""
    switch_user_plan_dev(db, test_user.id, "free")

    res_free = client.post(
        "/monitors",
        json={
            "name": "SSL Test",
            "type": "http",
            "url": "https://secure.example.com",
            "interval_seconds": 300,
            "ssl_monitoring_enabled": True,
        },
    )
    assert res_free.status_code == 403
    assert res_free.json()["detail"]["code"] == "PLAN_FEATURE_LOCKED"
    assert res_free.json()["detail"]["feature"] == "ssl_monitoring"

    # Switch to Pro
    switch_user_plan_dev(db, test_user.id, "pro")
    res_pro = client.post(
        "/monitors",
        json={
            "name": "SSL Test Pro",
            "type": "http",
            "url": "https://secure.example.com",
            "interval_seconds": 300,
            "ssl_monitoring_enabled": True,
        },
    )
    assert res_pro.status_code == 201
    assert res_pro.json()["ssl_monitoring_enabled"] is True


def test_legacy_ssl_configuration_remains_intact(db, test_user, client):
    """19. Existing grandfathered monitor with SSL enabled is preserved on Free plan updates."""
    switch_user_plan_dev(db, test_user.id, "free")

    # Manually insert legacy monitor with SSL enabled
    legacy_m = Monitor(
        user_id=test_user.id,
        type="http",
        name="Legacy SSL",
        url="https://legacy.example.com",
        interval_seconds=300,
        ssl_monitoring_enabled=True,
        ssl_status="valid",
    )
    db.add(legacy_m)
    db.commit()
    db.refresh(legacy_m)

    # Updating name should succeed without stripping SSL
    res = client.patch(f"/monitors/{legacy_m.id}", json={"name": "Legacy SSL Renamed"})
    assert res.status_code == 200
    assert res.json()["ssl_monitoring_enabled"] is True


def test_email_alerts_entitlement(db, test_user, client):
    """20, 21, 22. Free cannot enable email alerts; Pro can; in-app notifications still work."""
    switch_user_plan_dev(db, test_user.id, "free")

    res_toggle = client.patch("/settings/notifications", json={"email_enabled": True})
    assert res_toggle.status_code == 403
    assert res_toggle.json()["detail"]["code"] == "PLAN_FEATURE_LOCKED"

    res_test = client.post("/settings/notifications/test-email")
    assert res_test.status_code == 403
    assert res_test.json()["detail"]["code"] == "PLAN_FEATURE_LOCKED"

    # In-app notifications still work for Free users
    notif = Notification(
        user_id=test_user.id,
        title="Service degraded",
        message="Test alert",
        event_type="incident_opened",
        is_read=False,
    )
    db.add(notif)
    db.commit()

    res_notifs = client.get("/notifications")
    assert res_notifs.status_code == 200
    assert len(res_notifs.json()["items"]) >= 1

    # Pro can enable email alerts
    switch_user_plan_dev(db, test_user.id, "pro")
    res_toggle_pro = client.patch("/settings/notifications", json={"email_enabled": True})
    assert res_toggle_pro.status_code == 200
    assert res_toggle_pro.json()["email_enabled"] is True


def test_legacy_email_delivery_remains_intact(db, test_user, monkeypatch):
    """23 & 24. Legacy user with legacy_override sends email; deduplication intact."""
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_dummy_key")

    sub = switch_user_plan_dev(db, test_user.id, "free")
    sub.legacy_override = True
    db.commit()

    pref = NotificationPreference(
        user_id=test_user.id,
        email_enabled=True,
        http_down_enabled=True,
    )
    db.add(pref)

    notif = Notification(
        user_id=test_user.id,
        title="Down",
        message="Down msg",
        event_type="incident_opened",
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)

    delivery1 = enqueue_email_for_notification(db, notif)
    assert delivery1 is not None

    # Deduplication test
    delivery2 = enqueue_email_for_notification(db, notif)
    assert delivery2.id == delivery1.id


def test_status_page_limits(db, test_user, client):
    """25, 26, 27, 28, 29. Free allows 1 status page (including drafts); 2nd fails; Pro allows 5."""
    switch_user_plan_dev(db, test_user.id, "free")

    m = Monitor(
        user_id=test_user.id,
        type="http",
        name="SP Monitor",
        url="https://sp.example.com",
        interval_seconds=300,
    )
    db.add(m)
    db.commit()
    db.refresh(m)

    res1 = client.post(
        "/status-pages",
        json={
            "name": "Status Page 1",
            "slug": "page-one",
            "is_published": False,
            "monitors": [{"monitor_id": m.id}],
        },
    )
    assert res1.status_code == 201

    # 2nd page rejected on Free
    res2 = client.post(
        "/status-pages",
        json={
            "name": "Status Page 2",
            "slug": "page-two",
            "is_published": False,
            "monitors": [{"monitor_id": m.id}],
        },
    )
    assert res2.status_code == 403
    assert res2.json()["detail"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert res2.json()["detail"]["resource"] == "status_pages"

    # Switch to Pro allows up to 5
    switch_user_plan_dev(db, test_user.id, "pro")
    for i in range(2, 6):
        res = client.post(
            "/status-pages",
            json={
                "name": f"Status Page {i}",
                "slug": f"page-{i}",
                "is_published": False,
                "monitors": [{"monitor_id": m.id}],
            },
        )
        assert res.status_code == 201, f"Failed at page {i}: {res.text}"

    # 6th should fail on Pro
    res6 = client.post(
        "/status-pages",
        json={
            "name": "Status Page 6",
            "slug": "page-six",
            "is_published": False,
            "monitors": [{"monitor_id": m.id}],
        },
    )
    assert res6.status_code == 403
    assert res6.json()["detail"]["limit"] == 5


def test_tenant_isolation(db, test_user, other_user, client):
    """33 & 34. User A's monitors and quota cannot be consumed or manipulated by User B."""
    switch_user_plan_dev(db, test_user.id, "free")
    switch_user_plan_dev(db, other_user.id, "free")

    # User B has 3 monitors
    for i in range(3):
        m = Monitor(
            user_id=other_user.id,
            type="http",
            name=f"Other M {i}",
            url=f"https://other.example.com/{i}",
            interval_seconds=300,
        )
        db.add(m)
    db.commit()

    # User A currently has 0 monitors and should successfully create 3
    for i in range(3):
        res = client.post(
            "/monitors",
            json={
                "name": f"User A M {i}",
                "type": "http",
                "url": f"https://usera.example.com/{i}",
                "interval_seconds": 300,
            },
        )
        assert res.status_code == 201

    # Verify usage endpoints isolate accurately
    usage_a = get_subscription_usage(db, test_user.id)
    usage_b = get_subscription_usage(db, other_user.id)
    assert usage_a["http_monitors"]["used"] == 3
    assert usage_b["http_monitors"]["used"] == 3


def test_subscription_usage_api_endpoint(db, test_user, client):
    """GET /subscription/usage returns accurate usage and limits."""
    switch_user_plan_dev(db, test_user.id, "free")

    res = client.get("/subscription/usage")
    assert res.status_code == 200
    data = res.json()
    assert "http_monitors" in data
    assert "heartbeat_monitors" in data
    assert "status_pages" in data
    assert data["http_monitors"]["limit"] == 3
    assert data["heartbeat_monitors"]["limit"] == 1
    assert data["status_pages"]["limit"] == 1


def test_subscription_plans_endpoint(client):
    """GET /subscription/plans returns public plan configurations."""
    res = client.get("/subscription/plans")
    assert res.status_code == 200
    data = res.json()
    assert len(data["plans"]) == 2
    assert data["plans"][0]["id"] == "free"
    assert data["plans"][1]["id"] == "pro"
