import asyncio
import json
import os
import signal
import time
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


def record_worker_heartbeat(status: str, details: str | None = None) -> None:
    from app.database import SessionLocal
    from app.models.service_health import ServiceHealth

    db = SessionLocal()
    try:
        heartbeat = db.get(ServiceHealth, "monitor-worker")
        if heartbeat is None:
            heartbeat = ServiceHealth(service="monitor-worker")
        heartbeat.status = status
        heartbeat.details = details
        heartbeat.last_seen_at = datetime.now(timezone.utc)
        db.add(heartbeat)
        db.commit()
    except Exception as exc:
        db.rollback()
        print(
            json.dumps(
                {
                    "event": "worker_heartbeat_error",
                    "timestamp": utcnow_iso(),
                    "error": type(exc).__name__,
                }
            ),
            flush=True,
        )
    finally:
        db.close()


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

    last_lifecycle_check: float = 0.0

    while not shutdown_event.is_set():
        cycle_status = "healthy"
        cycle_error: str | None = None
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

            # Process pending transactional email outbox
            try:
                from app.services.email_service import process_pending_email_deliveries
                email_result = await asyncio.to_thread(process_pending_email_deliveries)
                if email_result.get("processed", 0) > 0:
                    print(
                        json.dumps(
                            {
                                "event": "email_cycle",
                                "timestamp": utcnow_iso(),
                                **email_result,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        flush=True,
                    )
            except Exception as email_exc:
                cycle_status = "degraded"
                cycle_error = f"email:{type(email_exc).__name__}"
                print(
                    json.dumps(
                        {
                            "event": "email_worker_error",
                            "timestamp": utcnow_iso(),
                            "error": str(email_exc)[:1000],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            # Run periodic subscription lifecycle maintenance (every 5 minutes / 300s)
            current_time = time.monotonic()
            if current_time - last_lifecycle_check >= 300:
                last_lifecycle_check = current_time
                try:
                    from app.services.subscription_service import run_subscription_lifecycle_jobs
                    lifecycle_result = await asyncio.to_thread(run_subscription_lifecycle_jobs)
                    if (
                        lifecycle_result.get("expirations", {}).get("expired", 0) > 0
                        or lifecycle_result.get("reminders", {}).get("reminders_queued", 0) > 0
                    ):
                        print(
                            json.dumps(
                                {
                                    "event": "subscription_lifecycle_cycle",
                                    "timestamp": utcnow_iso(),
                                    **lifecycle_result,
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            flush=True,
                        )
                except Exception as life_exc:
                    cycle_status = "degraded"
                    cycle_error = f"subscription:{type(life_exc).__name__}"
                    print(
                        json.dumps(
                            {
                                "event": "subscription_lifecycle_error",
                                "timestamp": utcnow_iso(),
                                "error": str(life_exc)[:1000],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        except Exception as exc:
            cycle_status = "degraded"
            cycle_error = type(exc).__name__
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

        # Record loop health for the admin API and host/container probes.
        await asyncio.to_thread(record_worker_heartbeat, cycle_status, cycle_error)
        try:
            heartbeat_path = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/raytech_worker_heartbeat")
            temp_path = f"{heartbeat_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as hf:
                hf.write(utcnow_iso())
            os.replace(temp_path, heartbeat_path)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "worker_heartbeat_file_error",
                        "timestamp": utcnow_iso(),
                        "error": type(exc).__name__,
                    }
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
