import asyncio
import json
import signal
from datetime import datetime, timezone

from app.services.monitor_runner import run_due_monitors
from app.services.ssl_service import run_due_ssl_checks
from app.services.heartbeat_service import run_due_heartbeat_checks


POLL_INTERVAL_SECONDS = 10

shutdown_event = asyncio.Event()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_shutdown():
    shutdown_event.set()


async def worker_loop():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                request_shutdown,
            )
        except NotImplementedError:
            pass

    print(
        json.dumps(
            {
                "event": "worker_started",
                "service": "raytech-monitor-worker",
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "timestamp": utcnow_iso(),
            }
        ),
        flush=True,
    )

    while not shutdown_event.is_set():
        try:
            result = await run_due_monitors()

            # Avoid filling logs every 10 seconds when nothing is due.
            if result.get("due", 0) > 0:
                print(
                    json.dumps(
                        {
                            "event": "monitor_cycle",
                            "timestamp": utcnow_iso(),
                            **result,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    flush=True,
                )

            ssl_result = await run_due_ssl_checks()
            if ssl_result.get("due", 0) > 0:
                print(
                    json.dumps(
                        {
                            "event": "ssl_cycle",
                            "timestamp": utcnow_iso(),
                            **ssl_result,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    flush=True,
                )

            heartbeat_result = await run_due_heartbeat_checks()
            if heartbeat_result.get("due", 0) > 0:
                print(
                    json.dumps(
                        {
                            "event": "heartbeat_cycle",
                            "timestamp": utcnow_iso(),
                            **heartbeat_result,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    flush=True,
                )

        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "worker_error",
                        "timestamp": utcnow_iso(),
                        "error": str(exc)[:1000],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass

    print(
        json.dumps(
            {
                "event": "worker_stopped",
                "service": "raytech-monitor-worker",
                "timestamp": utcnow_iso(),
            }
        ),
        flush=True,
    )


def main():
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
