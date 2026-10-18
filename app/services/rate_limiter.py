import ipaddress
import time
from collections import defaultdict
from typing import Any
from fastapi import HTTPException, Request, status


import os


def get_trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Retrieves explicitly configured trusted reverse proxy IP networks.

    By default, only loopback addresses (127.0.0.0/8, ::1/128) are trusted.
    Arbitrary RFC1918/private networks are strictly untrusted unless explicitly
    declared via the TRUSTED_PROXIES or TRUSTED_PROXY_IPS environment variable
    (e.g. Caddy container IP/CIDR on Docker network raytech-net).
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    ]
    raw_env = os.environ.get("TRUSTED_PROXIES") or os.environ.get("TRUSTED_PROXY_IPS")
    if raw_env:
        for entry in raw_env.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if "/" not in entry:
                    entry = f"{entry}/32" if ":" not in entry else f"{entry}/128"
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
    return tuple(networks)


def is_trusted_proxy(ip_str: str) -> bool:
    """Checks whether an IP address belongs to an explicitly configured trusted proxy network."""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str.strip())
        return any(ip in net for net in get_trusted_proxy_networks())
    except ValueError:
        return False


def get_trusted_client_ip(request: Request) -> str:
    """Securely extracts the true client IP address.

    Never blindly trusts X-Forwarded-For headers from untrusted connections.
    Only inspects X-Forwarded-For if the immediate connecting peer is an explicitly
    configured trusted reverse proxy. Direct requests or requests from untrusted
    Docker containers cannot spoof their identity.

    Traverses multiple proxy hops from right-to-left, discarding intermediate
    trusted proxies, and returns the first untrusted client IP.
    """
    direct_peer = (
        request.client.host
        if (request.client and request.client.host)
        else "127.0.0.1"
    )

    # If the direct peer is not an explicitly configured proxy, use direct peer IP
    if not is_trusted_proxy(direct_peer):
        return direct_peer

    # Peer is a trusted reverse proxy: parse X-Forwarded-For
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct_peer

    # X-Forwarded-For can be a comma-separated list of IPs (client, proxy1, proxy2...)
    # Traverse from right to left (closest proxy hop to origin client)
    raw_ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
    if not raw_ips:
        return direct_peer

    for candidate in reversed(raw_ips):
        try:
            cand_ip = ipaddress.ip_address(candidate)
        except ValueError:
            # Malformed candidate IP, fail safely and continue inspecting earlier hops
            continue

        cand_str = str(cand_ip)
        if is_trusted_proxy(cand_str):
            # Known intermediate trusted proxy, continue traversal to find client
            continue

        # Found the first non-trusted IP in the chain (the true client)
        return cand_str

    # If all entries in XFF were trusted proxies or malformed, fall back safely
    return direct_peer


class SlidingWindowRateLimiter:
    """Lightweight, in-memory sliding window rate limiter.

    Zero external infrastructure dependencies (no Redis required).
    Ideal for single-instance or containerized SaaS services.
    """

    def __init__(self, cleanup_interval_seconds: float = 300.0):
        self._history: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = cleanup_interval_seconds
        self.enabled_in_tests = False

    def is_allowed(
        self,
        key: str,
        max_requests: int,
        window_seconds: float,
    ) -> tuple[bool, int]:
        """Evaluates whether the request is allowed under the rate limit.

        Returns (allowed: bool, retry_after_seconds: int).
        """
        import os
        if not self.enabled_in_tests and os.environ.get("PYTEST_CURRENT_TEST"):
            return True, 0

        now = time.monotonic()
        self._maybe_cleanup(now)

        window_start = now - window_seconds
        timestamps = self._history[key]

        # Filter out timestamps outside the active window
        valid_timestamps = [t for t in timestamps if t > window_start]
        self._history[key] = valid_timestamps

        if len(valid_timestamps) >= max_requests:
            oldest_in_window = valid_timestamps[0]
            retry_after = max(1, int(window_seconds - (now - oldest_in_window)) + 1)
            return False, retry_after

        valid_timestamps.append(now)
        return True, 0

    def check_or_raise(
        self,
        key: str,
        max_requests: int,
        window_seconds: float,
        error_detail: str = "Rate limit exceeded. Please try again later.",
    ) -> None:
        """Enforces rate limit, raising HTTP 429 if threshold exceeded."""
        allowed, retry_after = self.is_allowed(key, max_requests, window_seconds)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": error_detail,
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically purges inactive keys to prevent memory growth."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        stale_threshold = now - 3600.0  # purge keys inactive for over 1 hour
        keys_to_delete = [
            k for k, timestamps in self._history.items()
            if not timestamps or timestamps[-1] < stale_threshold
        ]
        for k in keys_to_delete:
            del self._history[k]

    def reset_for_test(self) -> None:
        """Utility for test fixtures to clear rate limiting history."""
        self._history.clear()


# Global limiter instances
api_rate_limiter = SlidingWindowRateLimiter()
