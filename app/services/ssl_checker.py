import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import socket
import ssl
from urllib.parse import urlparse

from app.services.http_checker import UnsafeTargetError, _resolve_public_addresses


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SSLCheckResult:
    status: str  # 'valid' | 'expiring_soon' | 'expired' | 'invalid' | 'check_failed'
    hostname: str
    port: int
    issuer: str | None = None
    subject: str | None = None
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    days_remaining: int | None = None
    fingerprint_sha256: str | None = None
    serial_number: str | None = None
    error_type: str | None = None
    error_message: str | None = None


def _extract_rdn(entries: tuple, key: str) -> str | None:
    if not entries:
        return None
    for rdn in entries:
        for item in rdn:
            if len(item) == 2 and item[0] == key:
                return str(item[1])
    return None


def _parse_cert_info(cert: dict, der_bytes: bytes | None) -> dict:
    valid_from: datetime | None = None
    expires_at: datetime | None = None

    if "notBefore" in cert:
        try:
            ts = ssl.cert_time_to_seconds(cert["notBefore"])
            valid_from = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass

    if "notAfter" in cert:
        try:
            ts = ssl.cert_time_to_seconds(cert["notAfter"])
            expires_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass

    subject_entries = cert.get("subject", ())
    issuer_entries = cert.get("issuer", ())

    subject = _extract_rdn(subject_entries, "commonName")
    issuer = _extract_rdn(issuer_entries, "organizationName") or _extract_rdn(
        issuer_entries, "commonName"
    )

    serial_num = str(cert.get("serialNumber") or "") if cert.get("serialNumber") else None

    fingerprint = None
    if der_bytes and isinstance(der_bytes, (bytes, bytearray, memoryview)):
        fingerprint = hashlib.sha256(der_bytes).hexdigest().upper()

    return {
        "valid_from": valid_from,
        "expires_at": expires_at,
        "subject": subject,
        "issuer": issuer,
        "serial_number": serial_num,
        "fingerprint_sha256": fingerprint,
    }


def _probe_unverified_cert(
    resolved_ip: str,
    port: int,
    hostname: str,
    timeout_seconds: float,
) -> dict:
    """
    Optional metadata inspection for invalid/expired certificates.
    Connects to the already validated IP with TLS verification disabled
    SOLEY to extract issuer, subject, and expiry timestamps for UI diagnostics.
    THIS NEVER MARKS THE CERTIFICATE AS VALID.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    raw_sock = socket.socket(
        socket.AF_INET6 if ":" in resolved_ip else socket.AF_INET,
        socket.SOCK_STREAM,
    )
    raw_sock.settimeout(timeout_seconds)

    try:
        raw_sock.connect((resolved_ip, port))
        with ctx.wrap_socket(raw_sock, server_hostname=hostname) as ssock:
            der = ssock.getpeercert(binary_form=True)
            if not der:
                return {}
            # Under CERT_NONE, getpeercert() returns None, so we compute sha256
            # and can parse basic DER fields if needed or return fingerprint
            fingerprint = hashlib.sha256(der).hexdigest().upper()
            return {
                "fingerprint_sha256": fingerprint,
            }
    except Exception:
        return {}
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass


def _connect_and_check_ssl_sync(
    hostname: str,
    port: int,
    resolved_ips: list[str],
    warning_days: int,
    timeout_seconds: float,
) -> SSLCheckResult:
    """
    Synchronous socket connection and TLS verification.
    Connects to the pre-validated IP address to prevent DNS rebinding / TOCTOU,
    while specifying server_hostname for SNI and hostname verification.
    """
    last_error: Exception | None = None
    now = utcnow()

    # Try resolved public IPs
    for ip_str in resolved_ips:
        sock_family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
        raw_sock = socket.socket(sock_family, socket.SOCK_STREAM)
        raw_sock.settimeout(timeout_seconds)

        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        try:
            # Connect TCP directly to validated IP (prevents DNS rebinding)
            raw_sock.connect((ip_str, port))

            # Perform TLS handshake with hostname validation
            with context.wrap_socket(raw_sock, server_hostname=hostname) as ssock:
                der_bytes = ssock.getpeercert(binary_form=True)
                cert = ssock.getpeercert() or {}

                parsed = _parse_cert_info(cert, der_bytes)
                expires_at = parsed["expires_at"]

                if expires_at is None:
                    return SSLCheckResult(
                        status="invalid",
                        hostname=hostname,
                        port=port,
                        issuer=parsed["issuer"],
                        subject=parsed["subject"] or hostname,
                        valid_from=parsed["valid_from"],
                        expires_at=None,
                        days_remaining=None,
                        fingerprint_sha256=parsed["fingerprint_sha256"],
                        serial_number=parsed["serial_number"],
                        error_type="missing_expiry",
                        error_message="Certificate does not contain a valid expiration date",
                    )

                delta = (expires_at - now).total_seconds()
                days_remaining = int(delta // 86400)

                # Determine status
                if now > expires_at:
                    status = "expired"
                    error_type = "certificate_expired"
                    error_message = (
                        f"Certificate expired on {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    )
                elif days_remaining <= warning_days:
                    status = "expiring_soon"
                    error_type = None
                    error_message = None
                else:
                    status = "valid"
                    error_type = None
                    error_message = None

                return SSLCheckResult(
                    status=status,
                    hostname=hostname,
                    port=port,
                    issuer=parsed["issuer"] or "Unknown Issuer",
                    subject=parsed["subject"] or hostname,
                    valid_from=parsed["valid_from"],
                    expires_at=expires_at,
                    days_remaining=days_remaining,
                    fingerprint_sha256=parsed["fingerprint_sha256"],
                    serial_number=parsed["serial_number"],
                    error_type=error_type,
                    error_message=error_message,
                )

        except (ssl.SSLCertVerificationError, ssl.CertificateError) as exc:
            err_msg = str(exc)
            is_expired = "certificate has expired" in err_msg.lower() or getattr(exc, "verify_code", None) == 10

            unverified_info = _probe_unverified_cert(ip_str, port, hostname, timeout_seconds)

            return SSLCheckResult(
                status="expired" if is_expired else "invalid",
                hostname=hostname,
                port=port,
                issuer=None,
                subject=hostname,
                valid_from=None,
                expires_at=None,
                days_remaining=0 if is_expired else None,
                fingerprint_sha256=unverified_info.get("fingerprint_sha256"),
                serial_number=None,
                error_type="certificate_expired" if is_expired else "certificate_invalid",
                error_message=err_msg[:500],
            )

        except ssl.SSLError as exc:
            last_error = exc
            # Could be SSL protocol error, handshake failure
            continue

        except (socket.timeout, TimeoutError) as exc:
            last_error = exc
            continue

        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            last_error = exc
            continue

        finally:
            try:
                raw_sock.close()
            except Exception:
                pass

    # If all IPs failed without definitive cert verification error
    if isinstance(last_error, (socket.timeout, TimeoutError)):
        return SSLCheckResult(
            status="check_failed",
            hostname=hostname,
            port=port,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="timeout",
            error_message=f"Connection timed out after {timeout_seconds} seconds",
        )

    if isinstance(last_error, (ConnectionRefusedError, ConnectionResetError)):
        return SSLCheckResult(
            status="check_failed",
            hostname=hostname,
            port=port,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="connect_error",
            error_message=str(last_error)[:500],
        )

    return SSLCheckResult(
        status="check_failed",
        hostname=hostname,
        port=port,
        issuer=None,
        subject=None,
        valid_from=None,
        expires_at=None,
        days_remaining=None,
        fingerprint_sha256=None,
        serial_number=None,
        error_type="handshake_error" if isinstance(last_error, ssl.SSLError) else "network_error",
        error_message=str(last_error)[:500] if last_error else "Failed to establish TLS connection",
    )


async def check_ssl(
    *,
    url: str,
    warning_days: int = 14,
    timeout_seconds: int = 10,
) -> SSLCheckResult:
    """
    Asynchronously checks SSL certificate for a target HTTPS URL.
    Validates URL scheme, performs SSRF protection, prevents DNS rebinding,
    and returns a structured SSLCheckResult.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() != "https":
        return SSLCheckResult(
            status="check_failed",
            hostname=parsed.hostname or "",
            port=parsed.port or 443,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="invalid_target",
            error_message="Only HTTPS URLs are supported for SSL certificate monitoring",
        )

    if not parsed.hostname:
        return SSLCheckResult(
            status="check_failed",
            hostname="",
            port=443,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="invalid_target",
            error_message="URL does not contain a valid hostname",
        )

    if parsed.username or parsed.password:
        return SSLCheckResult(
            status="check_failed",
            hostname=parsed.hostname,
            port=parsed.port or 443,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="invalid_target",
            error_message="URLs containing credentials are not allowed",
        )

    hostname = parsed.hostname.lower()
    port = parsed.port or 443

    if hostname == "localhost" or hostname.endswith(".localhost"):
        return SSLCheckResult(
            status="check_failed",
            hostname=hostname,
            port=port,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="invalid_target",
            error_message="Localhost targets are not allowed",
        )

    # SSRF Protection: Resolve and validate IP addresses are public
    try:
        resolved_ips = await _resolve_public_addresses(hostname)
    except UnsafeTargetError as exc:
        return SSLCheckResult(
            status="check_failed",
            hostname=hostname,
            port=port,
            issuer=None,
            subject=None,
            valid_from=None,
            expires_at=None,
            days_remaining=None,
            fingerprint_sha256=None,
            serial_number=None,
            error_type="invalid_target",
            error_message=str(exc)[:500],
        )

    # Perform socket connection and certificate check on a worker thread
    return await asyncio.to_thread(
        _connect_and_check_ssl_sync,
        hostname=hostname,
        port=port,
        resolved_ips=resolved_ips,
        warning_days=warning_days,
        timeout_seconds=float(timeout_seconds),
    )
