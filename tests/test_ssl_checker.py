import asyncio
from datetime import datetime, timedelta, timezone
import socket
import ssl
from unittest.mock import MagicMock, patch
import pytest

from app.services.http_checker import UnsafeTargetError
from app.services.ssl_checker import (
    SSLCheckResult,
    check_ssl,
    _connect_and_check_ssl_sync,
)


@pytest.mark.asyncio
async def test_https_only_enforcement():
    result = await check_ssl(url="http://example.com/test")
    assert result.status == "check_failed"
    assert result.error_type == "invalid_target"
    assert "Only HTTPS URLs are supported" in (result.error_message or "")


@pytest.mark.asyncio
async def test_localhost_and_private_ip_rejection():
    # Localhost
    result = await check_ssl(url="https://localhost:443")
    assert result.status == "check_failed"
    assert "Localhost" in (result.error_message or "")

    result2 = await check_ssl(url="https://app.localhost/test")
    assert result2.status == "check_failed"
    assert "Localhost" in (result2.error_message or "")

    # Private IP target
    with patch(
        "app.services.ssl_checker._resolve_public_addresses",
        side_effect=UnsafeTargetError("Target resolves to a non-public address: 192.168.1.1"),
    ):
        result3 = await check_ssl(url="https://internal.corp/test")
        assert result3.status == "check_failed"
        assert result3.error_type == "invalid_target"
        assert "non-public address" in (result3.error_message or "")


@pytest.mark.asyncio
async def test_dns_failure():
    with patch(
        "app.services.ssl_checker._resolve_public_addresses",
        side_effect=UnsafeTargetError("Unable to resolve hostname: non-existent.test"),
    ):
        result = await check_ssl(url="https://non-existent.test")
        assert result.status == "check_failed"
        assert result.error_type == "invalid_target"
        assert "Unable to resolve hostname" in (result.error_message or "")


def test_valid_certificate():
    now = datetime.now(timezone.utc)
    future_expiry = now + timedelta(days=60)
    past_valid_from = now - timedelta(days=30)

    # Mock cert dict
    mock_cert = {
        "subject": ((("commonName", "example.com"),),),
        "issuer": ((("organizationName", "Let's Encrypt"),),),
        "notBefore": past_valid_from.strftime("%b %d %H:%M:%S %Y GMT"),
        "notAfter": future_expiry.strftime("%b %d %H:%M:%S %Y GMT"),
        "serialNumber": "0123456789ABCDEF",
    }
    mock_der = b"mock-der-binary-cert"

    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls, \
         patch("ssl.cert_time_to_seconds", side_effect=[past_valid_from.timestamp(), future_expiry.timestamp()]):

        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.side_effect = lambda binary_form=False: mock_der if binary_form else mock_cert
        mock_ssock.__enter__.return_value = mock_ssock

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock
        mock_ctx_cls.return_value = mock_ctx

        result = _connect_and_check_ssl_sync(
            hostname="example.com",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "valid"
        assert result.hostname == "example.com"
        assert result.issuer == "Let's Encrypt"
        assert result.subject == "example.com"
        assert result.days_remaining is not None and result.days_remaining >= 59
        assert result.fingerprint_sha256 is not None
        assert result.error_type is None


def test_expiring_soon_certificate():
    now = datetime.now(timezone.utc)
    future_expiry = now + timedelta(days=5)  # 5 days remaining, threshold 14 days
    past_valid_from = now - timedelta(days=85)

    mock_cert = {
        "subject": ((("commonName", "expiring.raytech.cloud"),),),
        "issuer": ((("organizationName", "DigiCert"),),),
        "notBefore": past_valid_from.strftime("%b %d %H:%M:%S %Y GMT"),
        "notAfter": future_expiry.strftime("%b %d %H:%M:%S %Y GMT"),
        "serialNumber": "987654321",
    }
    mock_der = b"expiring-der-bytes"

    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls, \
         patch("ssl.cert_time_to_seconds", side_effect=[past_valid_from.timestamp(), future_expiry.timestamp()]):

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.side_effect = lambda binary_form=False: mock_der if binary_form else mock_cert
        mock_ssock.__enter__.return_value = mock_ssock

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock
        mock_ctx_cls.return_value = mock_ctx

        result = _connect_and_check_ssl_sync(
            hostname="expiring.raytech.cloud",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "expiring_soon"
        assert result.days_remaining is not None and result.days_remaining <= 5
        assert result.issuer == "DigiCert"


def test_expired_certificate():
    now = datetime.now(timezone.utc)
    past_expiry = now - timedelta(days=2)  # Expired 2 days ago
    past_valid_from = now - timedelta(days=92)

    mock_cert = {
        "subject": ((("commonName", "expired.raytech.cloud"),),),
        "issuer": ((("organizationName", "ZeroSSL"),),),
        "notBefore": past_valid_from.strftime("%b %d %H:%M:%S %Y GMT"),
        "notAfter": past_expiry.strftime("%b %d %H:%M:%S %Y GMT"),
    }
    mock_der = b"expired-der-bytes"

    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls, \
         patch("ssl.cert_time_to_seconds", side_effect=[past_valid_from.timestamp(), past_expiry.timestamp()]):

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.side_effect = lambda binary_form=False: mock_der if binary_form else mock_cert
        mock_ssock.__enter__.return_value = mock_ssock

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock
        mock_ctx_cls.return_value = mock_ctx

        result = _connect_and_check_ssl_sync(
            hostname="expired.raytech.cloud",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "expired"
        assert result.error_type == "certificate_expired"
        assert "Certificate expired on" in (result.error_message or "")


def test_hostname_mismatch_verification_error():
    exc = ssl.SSLCertVerificationError(
        "hostname 'bad.com' doesn't match either of '*.example.com', 'example.com'"
    )

    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls:

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.side_effect = exc
        mock_ctx_cls.return_value = mock_ctx

        result = _connect_and_check_ssl_sync(
            hostname="bad.com",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "invalid"
        assert result.error_type == "certificate_invalid"
        assert "doesn't match" in (result.error_message or "")


def test_self_signed_certificate():
    exc = ssl.SSLCertVerificationError("self-signed certificate in certificate chain")

    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls:

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.side_effect = exc
        mock_ctx_cls.return_value = mock_ctx

        result = _connect_and_check_ssl_sync(
            hostname="self-signed.local",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "invalid"
        assert result.error_type == "certificate_invalid"
        assert "self-signed certificate" in (result.error_message or "")


def test_tls_handshake_failure():
    exc = ssl.SSLError("SSLV3_ALERT_HANDSHAKE_FAILURE")

    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls:

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.side_effect = exc
        mock_ctx_cls.return_value = mock_ctx

        result = _connect_and_check_ssl_sync(
            hostname="tls-fail.com",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "check_failed"
        assert result.error_type == "handshake_error"


def test_connection_timeout():
    exc = socket.timeout("timed out")

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = exc
        mock_sock_cls.return_value = mock_sock

        result = _connect_and_check_ssl_sync(
            hostname="timeout.com",
            port=443,
            resolved_ips=["93.184.216.34"],
            warning_days=14,
            timeout_seconds=5.0,
        )

        assert result.status == "check_failed"
        assert result.error_type == "timeout"
        assert "timed out" in (result.error_message or "")


def test_dns_rebinding_protection():
    """
    Ensure the actual TCP socket.connect connects to the resolved IP (not the hostname),
    while wrap_socket uses server_hostname for SNI and hostname check.
    """
    mock_cert = {
        "subject": ((("commonName", "secure.com"),),),
        "notBefore": "Aug 01 00:00:00 2026 GMT",
        "notAfter": "Aug 01 00:00:00 2027 GMT",
    }
    with patch("socket.socket") as mock_sock_cls, \
         patch("ssl.create_default_context") as mock_ctx_cls, \
         patch("ssl.cert_time_to_seconds", side_effect=[1785542400, 1817078400]):

        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.side_effect = lambda binary_form=False: b"test_der_cert" if binary_form else mock_cert
        mock_ssock.__enter__.return_value = mock_ssock

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock
        mock_ctx_cls.return_value = mock_ctx

        target_ip = "198.51.100.1"
        result = _connect_and_check_ssl_sync(
            hostname="secure.com",
            port=443,
            resolved_ips=[target_ip],
            warning_days=14,
            timeout_seconds=5.0,
        )

        # Verified that raw_sock.connect connected directly to target_ip
        mock_sock.connect.assert_called_once_with((target_ip, 443))

        # Verified that wrap_socket received server_hostname="secure.com"
        mock_ctx.wrap_socket.assert_called_once_with(mock_sock, server_hostname="secure.com")
        assert result.status == "valid"
