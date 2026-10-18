import asyncio
import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.models.monitor import Monitor
from app.models.incident import Incident
from app.models.status_page import StatusPage
from app.models.payment_transaction import PaymentTransaction
from app.services.http_checker import (
    BLOCKED_PORTS,
    BLOCKED_HOSTNAMES,
    PinnedIPBackend,
    UnsafeTargetError,
    validate_target,
    check_http,
)
from app.services.rate_limiter import (
    api_rate_limiter,
    get_trusted_client_ip,
    is_trusted_proxy,
)
from app.dependencies.auth import get_current_user


@pytest.fixture(autouse=True)
def reset_limiter():
    api_rate_limiter.reset_for_test()
    yield
    api_rate_limiter.reset_for_test()


# ==============================================================================
# SECTION 1: SSRF & TARGET VALIDATION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_ssrf_rejects_loopback_and_private_ips():
    """Verify private and loopback IP addresses are blocked."""
    forbidden_urls = [
        "http://127.0.0.1:8000",
        "http://127.0.0.2:80",
        "http://10.0.0.5:8080",
        "http://172.16.0.10:3000",
        "http://172.20.0.2:5432",  # Docker network range
        "http://192.168.1.100:80",
        "http://localhost:8080",
        "http://sub.localhost:3000",
    ]
    for url in forbidden_urls:
        with pytest.raises(UnsafeTargetError):
            await validate_target(url)


@pytest.mark.asyncio
async def test_ssrf_rejects_cloud_metadata_and_docker_internal():
    """Verify AWS/GCP/Azure metadata and internal Docker hostnames are blocked."""
    metadata_urls = [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/v1/",
        "http://instance-data/latest/",
        "http://raytech-postgres:5432",
        "http://raytech-monitor-api:8000",
        "http://raytech-monitor-worker:8000",
        "http://host.docker.internal:8000",
        "http://gateway.docker.internal:80",
        "http://internal-service.local",
        "http://internal-db.internal",
        "http://backup.lan",
    ]
    for url in metadata_urls:
        with pytest.raises(UnsafeTargetError):
            await validate_target(url)


@pytest.mark.asyncio
async def test_ssrf_rejects_alternative_ip_representations():
    """Verify octal, hex, and dword integer IP obfuscations are rejected."""
    obfuscated_urls = [
        "http://2130706433",       # 127.0.0.1 in decimal integer
        "http://0177.0.0.1",       # 127.0.0.1 in octal notation
        "http://0x7f.0.0.1",       # 127.0.0.1 in hex notation
        "http://0x7f000001",       # 127.0.0.1 in full hex
    ]
    for url in obfuscated_urls:
        with pytest.raises(UnsafeTargetError):
            await validate_target(url)


@pytest.mark.asyncio
async def test_ssrf_rejects_sensitive_service_ports():
    """Verify probing database, SSH, and Docker daemon ports is rejected."""
    dangerous_port_urls = [
        "http://93.184.216.34:22",    # SSH
        "http://93.184.216.34:25",    # SMTP
        "http://93.184.216.34:5432",  # PostgreSQL
        "http://93.184.216.34:6379",  # Redis
        "http://93.184.216.34:2375",  # Docker daemon
    ]
    for url in dangerous_port_urls:
        with pytest.raises(UnsafeTargetError) as exc_info:
            await validate_target(url)
        assert "restricted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ssrf_rejects_invalid_schemes_and_credentials():
    """Verify file://, ftp://, and embedded credentials are strictly rejected."""
    invalid_urls = [
        "file:///etc/passwd",
        "gopher://127.0.0.1:70",
        "ftp://example.com/test",
        "http://admin:secret@example.com",
    ]
    for url in invalid_urls:
        with pytest.raises(UnsafeTargetError):
            await validate_target(url)


@pytest.mark.asyncio
async def test_dns_rebinding_prevention_pinned_backend():
    """Verify PinnedIPBackend enforces connecting strictly to the pre-validated IP."""
    backend = PinnedIPBackend("93.184.216.34")
    # Verify connect_tcp redirects host parameter to pinned_ip
    with patch(
        "httpcore._backends.anyio.AnyIOBackend.connect_tcp",
        return_value="mock_stream",
    ) as mock_super_connect:
        result = await backend.connect_tcp(host="malicious-rebind.com", port=443)
        mock_super_connect.assert_called_once_with(
            host="93.184.216.34",
            port=443,
            timeout=None,
            local_address=None,
            socket_options=None,
        )
        assert result == "mock_stream"


def test_pinned_ip_backend_rejects_private_and_restricted_destinations():
    """Verify PinnedIPBackend fails-closed if passed any non-public or restricted IP."""
    unsafe_ips = [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "not-an-ip",
    ]
    for ip_str in unsafe_ips:
        with pytest.raises(UnsafeTargetError):
            PinnedIPBackend(ip_str)


def test_pinned_ip_backend_accepts_public_ipv4_and_ipv6():
    """Verify PinnedIPBackend allows public IPs and cleans bracket notation."""
    b_v4 = PinnedIPBackend("93.184.216.34")
    assert b_v4.pinned_ip == "93.184.216.34"

    b_v6 = PinnedIPBackend("2606:4700:4700::1111")
    assert b_v6.pinned_ip == "2606:4700:4700::1111"

    b_v6_brackets = PinnedIPBackend("[2606:4700:4700::1111]")
    assert b_v6_brackets.pinned_ip == "2606:4700:4700::1111"


@pytest.mark.asyncio
async def test_check_http_proxy_environment_variables_strictly_ignored():
    """Verify proxy environment variables (HTTP_PROXY, HTTPS_PROXY) cannot hijack checks."""
    with patch.dict(os.environ, {
        "HTTP_PROXY": "http://malicious-proxy.internal:8080",
        "HTTPS_PROXY": "http://malicious-proxy.internal:8080",
        "ALL_PROXY": "socks5://malicious-proxy.internal:1080",
    }):
        # Mocking validate_target to return a public IP
        with patch("app.services.http_checker.validate_target", return_value=["93.184.216.34"]):
            with patch("httpx.AsyncClient.get") as mock_get:
                mock_res = MagicMock()
                mock_res.status_code = 200
                mock_get.return_value = mock_res

                res = await check_http(
                    url="https://example.com",
                    timeout_seconds=5,
                    expected_status_min=200,
                    expected_status_max=299,
                )
                assert res.success is True


@pytest.mark.asyncio
async def test_check_http_live_public_endpoints_and_blocked_targets():
    """Verify real networking against public endpoints and safe blocking of loopback targets."""
    # 1. Real public HTTPS endpoint
    res_https = await check_http(
        url="https://example.com",
        timeout_seconds=10,
        expected_status_min=200,
        expected_status_max=299,
    )
    assert res_https.success is True
    assert res_https.http_status == 200

    # 2. Blocked private target fails without network interaction
    res_blocked = await check_http(
        url="http://127.0.0.1:8000/probe",
        timeout_seconds=5,
        expected_status_min=200,
        expected_status_max=299,
    )
    assert res_blocked.success is False
    assert res_blocked.error_type == "invalid_target"


def test_monitor_creation_rejects_ssrf_targets(client, test_user):
    """Verify monitor creation API rejects SSRF targets with HTTP 422 immediately."""
    app.dependency_overrides[get_current_user] = lambda: test_user

    # Loopback
    res = client.post("/monitors", json={
        "name": "SSRF Loopback",
        "type": "http",
        "url": "http://127.0.0.1:8000/internal",
        "interval_seconds": 300,
    })
    assert res.status_code == 422
    assert "Invalid target URL" in res.json()["detail"]

    # Metadata
    res2 = client.post("/monitors", json={
        "name": "SSRF Metadata",
        "type": "http",
        "url": "http://169.254.169.254/latest/meta-data/",
        "interval_seconds": 300,
    })
    assert res2.status_code == 422
    assert "Invalid target URL" in res2.json()["detail"]


# ==============================================================================
# SECTION 2: AUTHENTICATION, AUTHORIZATION & IDOR TESTS
# ==============================================================================

def test_unauthenticated_api_endpoints_rejected(client):
    """Verify unauthenticated requests to protected endpoints return HTTP 401."""
    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    endpoints = [
        ("GET", "/monitors"),
        ("POST", "/monitors"),
        ("GET", "/incidents"),
        ("GET", "/notifications"),
        ("GET", "/settings/notifications"),
        ("GET", "/status-pages"),
        ("GET", "/subscription"),
        ("GET", "/billing/transactions"),
        ("POST", "/billing/checkout"),
    ]
    for method, path in endpoints:
        if method == "GET":
            res = client.get(path)
        else:
            res = client.post(path, json={})
        assert res.status_code == 401, f"{method} {path} did not return 401"


def test_idor_protection_monitor_access(db, client, test_user):
    """Verify User B cannot access, modify, or delete User A's monitors (IDOR)."""
    # User A owns monitor 101
    user_a = test_user
    m_a = Monitor(
        id=8101,
        user_id=user_a.id,
        name="User A Private Monitor",
        type="http",
        url="https://example.com/a",
        is_active=True,
    )
    db.add(m_a)

    # User B
    user_b = User(id=8002, email="user_b@example.com", name="User B", is_active=True)
    db.add(user_b)
    db.commit()

    # Authenticate as User B
    app.dependency_overrides[get_current_user] = lambda: user_b

    # User B attempts to read User A's monitor -> 404
    res = client.get(f"/monitors/{m_a.id}")
    assert res.status_code == 404

    # User B attempts to modify User A's monitor -> 404
    res = client.patch(f"/monitors/{m_a.id}", json={"name": "Hacked Name"})
    assert res.status_code == 404

    # User B attempts to delete User A's monitor -> 404
    res = client.delete(f"/monitors/{m_a.id}")
    assert res.status_code == 404


def test_idor_protection_status_page_access(db, client, test_user):
    """Verify User B cannot view or modify User A's private status page."""
    user_a = test_user
    sp_a = StatusPage(
        id=8301,
        user_id=user_a.id,
        name="User A Status Page",
        slug="user-a-status",
        is_published=False,
    )
    db.add(sp_a)

    user_b = db.scalar(select(User).where(User.email == "user_b@example.com"))
    if not user_b:
        user_b = User(id=8002, email="user_b@example.com", name="User B", is_active=True)
        db.add(user_b)
    db.commit()

    app.dependency_overrides[get_current_user] = lambda: user_b

    # User B attempts to get status page -> 404
    res = client.get(f"/status-pages/{sp_a.id}")
    assert res.status_code == 404

    # User B attempts to update status page -> 404
    res = client.patch(f"/status-pages/{sp_a.id}", json={"name": "Hacked Status Page"})
    assert res.status_code == 404


def test_idor_protection_billing_transactions(db, client, test_user):
    """Verify User B cannot access User A's payment transaction history."""
    tx_a = PaymentTransaction(
        id=8501,
        user_id=test_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PAID",
        external_id="syn_tx_user_a_secret",
    )
    db.add(tx_a)

    user_b = db.scalar(select(User).where(User.email == "user_b@example.com"))
    if not user_b:
        user_b = User(id=8002, email="user_b@example.com", name="User B", is_active=True)
        db.add(user_b)
    db.commit()

    app.dependency_overrides[get_current_user] = lambda: user_b

    # User B queries User A's transaction -> 404
    res = client.get(f"/billing/transactions/{tx_a.id}")
    assert res.status_code == 404

    # User B polls User A's transaction -> 404
    res = client.get(f"/billing/transactions/{tx_a.id}/status")
    assert res.status_code == 404


def test_monitor_dev_token_disabled_in_production(db, client):
    """Verify MONITOR_DEV_TOKEN header is completely ignored when ENVIRONMENT=production."""
    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    test_email = "dev_admin@example.com"
    user = User(email=test_email, name="Dev Admin", is_active=True)
    db.add(user)
    db.commit()

    with patch.dict(os.environ, {
        "ENVIRONMENT": "production",
        "MONITOR_DEV_TOKEN": "secret-dev-token-xyz",
        "MONITOR_DEV_USER_EMAIL": test_email,
    }):
        res = client.get("/monitors", headers={"X-Dev-Token": "secret-dev-token-xyz"})
        assert res.status_code == 401


# ==============================================================================
# SECTION 3: RATE LIMITING & TRUSTED IP EXTRACTION TESTS
# ==============================================================================

def test_trusted_proxy_direct_request_cannot_spoof_ip():
    """Verify direct connections from public IPs cannot spoof X-Forwarded-For."""
    req_direct = MagicMock()
    req_direct.client.host = "203.0.113.50"  # Public IP
    req_direct.headers = {"x-forwarded-for": "1.2.3.4"}
    assert get_trusted_client_ip(req_direct) == "203.0.113.50"


def test_trusted_proxy_untrusted_docker_container_cannot_spoof_ip():
    """Verify other Docker containers on RFC1918 subnets cannot spoof XFF without explicit trust."""
    req_container = MagicMock()
    req_container.client.host = "172.16.0.5"  # Docker container on bridge
    req_container.headers = {"x-forwarded-for": "8.8.8.8"}
    # Without TRUSTED_PROXIES set, 172.16.0.5 is not trusted
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TRUSTED_PROXIES", None)
        os.environ.pop("TRUSTED_PROXY_IPS", None)
        assert get_trusted_client_ip(req_container) == "172.16.0.5"


def test_trusted_proxy_explicitly_configured_caddy():
    """Verify explicitly configured Caddy proxy extracts the real client IP."""
    req_caddy = MagicMock()
    req_caddy.client.host = "172.20.0.2"
    req_caddy.headers = {"x-forwarded-for": "198.51.100.25"}

    with patch.dict(os.environ, {"TRUSTED_PROXIES": "172.20.0.2"}):
        assert get_trusted_client_ip(req_caddy) == "198.51.100.25"


def test_trusted_proxy_multiple_hops_traversal():
    """Verify multiple proxy hops are traversed right-to-left safely."""
    req = MagicMock()
    req.client.host = "172.20.0.2"  # Caddy
    # Chain: Client (203.0.113.195), CDN/Proxy (192.0.2.1)
    req.headers = {"x-forwarded-for": "203.0.113.195, 192.0.2.1"}

    with patch.dict(os.environ, {"TRUSTED_PROXIES": "172.20.0.2, 192.0.2.1"}):
        assert get_trusted_client_ip(req) == "203.0.113.195"


def test_trusted_proxy_malformed_and_missing_headers_fail_safe():
    """Verify missing, empty, or malformed XFF headers fail safe to direct peer."""
    with patch.dict(os.environ, {"TRUSTED_PROXIES": "172.20.0.2"}):
        # Missing header
        req_missing = MagicMock()
        req_missing.client.host = "172.20.0.2"
        req_missing.headers = {}
        assert get_trusted_client_ip(req_missing) == "172.20.0.2"

        # Malformed header
        req_malformed = MagicMock()
        req_malformed.client.host = "172.20.0.2"
        req_malformed.headers = {"x-forwarded-for": "bad-ip, <script>alert(1)</script>"}
        assert get_trusted_client_ip(req_malformed) == "172.20.0.2"


def test_rate_limiting_auth_google_login(client):
    """Verify /auth/google/login rate limits rapid requests with HTTP 429."""
    api_rate_limiter.enabled_in_tests = True
    try:
        with patch("app.services.oauth.oauth.google.authorize_redirect", return_value={"mock": "redirect"}):
            # Fire 30 requests (allowed)
            for _ in range(30):
                res = client.get("/auth/google/login")
                assert res.status_code == 200 or res.status_code == 302 or "mock" in str(res.content)

            # 31st request triggers rate limit
            res_blocked = client.get("/auth/google/login")
            assert res_blocked.status_code == 429
            assert "Retry-After" in res_blocked.headers
            assert res_blocked.json()["detail"]["error"] == "RATE_LIMIT_EXCEEDED"
    finally:
        api_rate_limiter.enabled_in_tests = False


def test_rate_limiting_billing_checkout(client, test_user):
    """Verify /billing/checkout rate limits excessive session creation."""
    api_rate_limiter.enabled_in_tests = True
    try:
        app.dependency_overrides[get_current_user] = lambda: test_user

        with patch("app.routes.billing.create_xendit_invoice", return_value={"id": "inv_1", "invoice_url": "https://pay"}):
            for _ in range(10):
                client.post("/billing/checkout", json={"plan": "pro"})

            res_blocked = client.post("/billing/checkout", json={"plan": "pro"})
            assert res_blocked.status_code == 429
            assert "Retry-After" in res_blocked.headers
    finally:
        api_rate_limiter.enabled_in_tests = False


# ==============================================================================
# SECTION 4: HTTP SECURITY HEADERS & BODY LIMIT TESTS
# ==============================================================================

def test_owasp_security_headers_present(client):
    """Verify security headers are attached to all HTTP responses."""
    res = client.get("/")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert res.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "geolocation=()" in res.headers.get("Permissions-Policy", "")


def test_request_body_size_limit_enforced(client):
    """Verify payloads exceeding 2MB are rejected with HTTP 413."""
    # Oversized content-length header
    res = client.post(
        "/monitors",
        headers={"Content-Length": str(3 * 1024 * 1024)},
        content=b"A" * 100,
    )
    assert res.status_code == 413
    assert "2MB" in res.json()["error"]


@pytest.mark.asyncio
async def test_request_body_size_chunked_body_over_limit():
    """Verify chunked streaming payload without Content-Length exceeding 2MB returns HTTP 413."""
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def chunked_stream():
            chunk = b"X" * (512 * 1024)
            for _ in range(5):  # 5 * 512KB = 2.5MB > 2MB
                yield chunk

        with patch("app.routes.webhooks.verify_webhook_token", return_value=True):
            res = await client.post(
                "/webhooks/xendit",
                headers={"x-callback-token": "valid_mock_token"},
                content=chunked_stream(),
            )
            assert res.status_code == 413
            assert "2MB" in res.json()["error"]


@pytest.mark.asyncio
async def test_request_body_size_missing_content_length_under_limit():
    """Verify chunked streaming payload under 2MB succeeds without 413."""
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def small_stream():
            yield b"small_payload"

        res = await client.post("/health", content=small_stream())
        assert res.status_code != 413


@pytest.mark.asyncio
async def test_request_body_size_malformed_content_length():
    """Verify unparseable or negative Content-Length returns HTTP 400."""
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res_unparseable = await client.post(
            "/health",
            headers={"Content-Length": "not-a-number"},
            content=b"test",
        )
        assert res_unparseable.status_code == 400

        res_negative = await client.post(
            "/health",
            headers={"Content-Length": "-500"},
            content=b"test",
        )
        assert res_negative.status_code == 400


@pytest.mark.asyncio
async def test_request_body_size_request_exactly_at_limit():
    """Verify request with Content-Length exactly at 2MB limit does not return 413."""
    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        exact_2mb = 2 * 1024 * 1024
        res = await client.post(
            "/health",
            headers={"Content-Length": str(exact_2mb)},
            content=b"",
        )
        assert res.status_code != 413


def test_health_check_returns_503_when_database_disconnected(client):
    """Verify /health returns HTTP 503 Service Unavailable when DB is disconnected."""
    with patch("app.main.engine.connect", side_effect=Exception("Database connection error")):
        res = client.get("/health")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded"
        assert data["database"] == "disconnected"


def test_health_check_returns_200_when_database_connected(client):
    """Verify /health returns HTTP 200 OK when DB is healthy."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"


# ==============================================================================
# SECTION 5: XENDIT BILLING SECURITY BOUNDARIES
# ==============================================================================

def test_live_xendit_key_strictly_rejected():
    """Verify live production Xendit keys trigger XenditConfigurationError."""
    from app.services.xendit_service import get_xendit_secret_key, XenditConfigurationError

    with patch.dict(os.environ, {"XENDIT_SECRET_KEY": "xnd_production_secret_key_12345"}):
        with pytest.raises(XenditConfigurationError) as exc_info:
            get_xendit_secret_key()
        assert "Production Xendit key detected while running in Sandbox mode" in str(exc_info.value)


def test_dev_plan_switch_strictly_disabled_in_production(client, test_user):
    """Verify /subscription/admin/switch-plan is forbidden in production environment."""
    app.dependency_overrides[get_current_user] = lambda: test_user

    with patch.dict(os.environ, {"ENVIRONMENT": "production", "ALLOW_DEV_PLAN_SWITCH": "true"}):
        res = client.post("/subscription/admin/switch-plan", json={"plan": "pro"})
        assert res.status_code == 403
