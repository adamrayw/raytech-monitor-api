import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


@dataclass
class CheckResult:
    success: bool
    http_status: int | None
    response_ms: float | None
    error_type: str | None
    error_message: str | None


class UnsafeTargetError(Exception):
    pass


async def _resolve_public_addresses(hostname: str) -> list[str]:
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            None,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeTargetError(
            f"Unable to resolve hostname: {hostname}"
        ) from exc

    addresses: set[str] = set()

    for info in infos:
        address = info[4][0]

        # IPv6 address may contain zone identifier.
        address = address.split("%", 1)[0]

        addresses.add(address)

    if not addresses:
        raise UnsafeTargetError(
            f"No IP addresses resolved for hostname: {hostname}"
        )

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeTargetError(
                f"Invalid resolved IP address: {address}"
            ) from exc

        # Blocks loopback, private, link-local, multicast,
        # reserved, unspecified, etc.
        if not ip.is_global:
            raise UnsafeTargetError(
                f"Target resolves to a non-public address: {address}"
            )

    return sorted(addresses)


async def validate_target(url: str) -> None:
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise UnsafeTargetError(
            "Only HTTP and HTTPS URLs are allowed"
        )

    if not parsed.hostname:
        raise UnsafeTargetError(
            "URL does not contain a valid hostname"
        )

    if parsed.username or parsed.password:
        raise UnsafeTargetError(
            "URLs containing credentials are not allowed"
        )

    hostname = parsed.hostname.lower()

    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeTargetError(
            "Localhost targets are not allowed"
        )

    await _resolve_public_addresses(hostname)


async def check_http(
    *,
    url: str,
    timeout_seconds: int,
    expected_status_min: int,
    expected_status_max: int,
) -> CheckResult:
    try:
        await validate_target(url)

    except UnsafeTargetError as exc:
        return CheckResult(
            success=False,
            http_status=None,
            response_ms=None,
            error_type="invalid_target",
            error_message=str(exc)[:500],
        )

    timeout = httpx.Timeout(
        timeout=float(timeout_seconds),
        connect=float(timeout_seconds),
        read=float(timeout_seconds),
        write=float(timeout_seconds),
        pool=float(timeout_seconds),
    )

    started = time.perf_counter()

    try:
        # Redirects deliberately disabled for v1.
        # Otherwise a public URL could redirect to an internal address.
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "User-Agent": "Raytech-Monitor/0.1",
                "Accept": "*/*",
            },
        ) as client:
            response = await client.get(url)

        response_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        valid_status = (
            expected_status_min
            <= response.status_code
            <= expected_status_max
        )

        if not valid_status:
            return CheckResult(
                success=False,
                http_status=response.status_code,
                response_ms=response_ms,
                error_type="unexpected_status",
                error_message=(
                    f"Expected HTTP "
                    f"{expected_status_min}-{expected_status_max}, "
                    f"received {response.status_code}"
                ),
            )

        return CheckResult(
            success=True,
            http_status=response.status_code,
            response_ms=response_ms,
            error_type=None,
            error_message=None,
        )

    except httpx.TimeoutException:
        response_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        return CheckResult(
            success=False,
            http_status=None,
            response_ms=response_ms,
            error_type="timeout",
            error_message=(
                f"Request timed out after {timeout_seconds} seconds"
            ),
        )

    except httpx.ConnectError as exc:
        response_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        return CheckResult(
            success=False,
            http_status=None,
            response_ms=response_ms,
            error_type="connect_error",
            error_message=str(exc)[:500],
        )

    except httpx.RequestError as exc:
        response_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        return CheckResult(
            success=False,
            http_status=None,
            response_ms=response_ms,
            error_type="request_error",
            error_message=str(exc)[:500],
        )

    except Exception as exc:
        response_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        return CheckResult(
            success=False,
            http_status=None,
            response_ms=response_ms,
            error_type="internal_error",
            error_message=str(exc)[:500],
        )
