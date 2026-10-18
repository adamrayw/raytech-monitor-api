"""
Raytech Monitor — Incident Consistency & Reconciliation Utility
Audit and safely reconcile stale / orphaned incidents without mass updates or false alerts.

Usage:
    python -m scripts.reconcile_incidents           # Dry run (default, report only)
    python -m scripts.reconcile_incidents --dry-run # Explicit dry run
    python -m scripts.reconcile_incidents --fix     # Apply safe reconciliation
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env file automatically if DATABASE_URL is not set in environment
if "DATABASE_URL" not in os.environ:
    for env_path in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
            break

# If still not set (e.g. CLI invocation with --help or unit testing), default to dummy sqlite
if "DATABASE_URL" not in os.environ:
    if "--help" in sys.argv or "-h" in sys.argv:
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    else:
        print("ERROR: DATABASE_URL environment variable is not set. Please set it or create a .env file.", file=sys.stderr)
        sys.exit(1)

from sqlalchemy import select

from app.database import SessionLocal
from app.models.incident import Incident
from app.models.monitor import Monitor


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def inspect_incidents(db):
    """
    Scans all open incidents and detects inconsistencies with their monitor's current state.
    Returns a list of diagnostic records.
    """
    open_incidents = list(
        db.scalars(
            select(Incident)
            .where(Incident.status == "open")
            .order_by(Incident.started_at.asc())
        ).all()
    )

    records = []

    for inc in open_incidents:
        monitor = db.get(Monitor, inc.monitor_id)

        if monitor is None:
            records.append({
                "incident_id": inc.id,
                "monitor_id": inc.monitor_id,
                "monitor_name": "[Deleted Monitor]",
                "monitor_type": "unknown",
                "monitor_status": "deleted",
                "monitor_is_active": False,
                "incident_status": inc.status,
                "incident_started_at": inc.started_at,
                "incident_cause": inc.cause,
                "is_inconsistent": True,
                "inconsistency_reason": "Orphaned incident: Monitor was deleted",
                "proposed_action": "RESOLVE_ORPHAN",
                "resolved_at": inc.started_at,
                "resolution_reason": "reconciled_orphaned_incident",
            })
            continue

        # Case 1: Monitor is healthy/operational/degraded
        # An active service MUST NOT have an open downtime incident!
        if monitor.status in {"operational", "degraded", "healthy"}:
            resolved_at = monitor.last_success_at or inc.started_at
            if resolved_at and resolved_at < inc.started_at:
                resolved_at = utcnow()

            records.append({
                "incident_id": inc.id,
                "monitor_id": monitor.id,
                "monitor_name": monitor.name,
                "monitor_type": monitor.type,
                "monitor_status": monitor.status,
                "monitor_is_active": monitor.is_active,
                "incident_status": inc.status,
                "incident_started_at": inc.started_at,
                "incident_cause": inc.cause,
                "is_inconsistent": True,
                "inconsistency_reason": (
                    f"Monitor is currently {monitor.status.upper()} (healthy), "
                    f"but incident was left open due to unhandled transition / state reset"
                ),
                "proposed_action": "RESOLVE_STALE",
                "resolved_at": resolved_at,
                "resolution_reason": "reconciled_stale_incident",
            })

        # Case 2: Monitor is legitimately down or missed
        elif monitor.status in {"down", "missed"}:
            records.append({
                "incident_id": inc.id,
                "monitor_id": monitor.id,
                "monitor_name": monitor.name,
                "monitor_type": monitor.type,
                "monitor_status": monitor.status,
                "monitor_is_active": monitor.is_active,
                "incident_status": inc.status,
                "incident_started_at": inc.started_at,
                "incident_cause": inc.cause,
                "is_inconsistent": False,
                "inconsistency_reason": "Legitimate active disruption; monitor is down/missed",
                "proposed_action": "KEEP_OPEN",
                "resolved_at": None,
                "resolution_reason": None,
            })

        # Case 3: Monitor is paused while incident was open
        elif not monitor.is_active or monitor.status == "paused":
            records.append({
                "incident_id": inc.id,
                "monitor_id": monitor.id,
                "monitor_name": monitor.name,
                "monitor_type": monitor.type,
                "monitor_status": "paused",
                "monitor_is_active": False,
                "incident_status": inc.status,
                "incident_started_at": inc.started_at,
                "incident_cause": inc.cause,
                "is_inconsistent": True,
                "inconsistency_reason": "Monitor is paused with an open incident",
                "proposed_action": "RESOLVE_PAUSED",
                "resolved_at": monitor.updated_at or inc.started_at,
                "resolution_reason": "reconciled_paused_monitor",
            })

        # Case 4: Monitor status unknown / pending (e.g. freshly created or resumed, pending first check)
        else:
            records.append({
                "incident_id": inc.id,
                "monitor_id": monitor.id,
                "monitor_name": monitor.name,
                "monitor_type": monitor.type,
                "monitor_status": monitor.status,
                "monitor_is_active": monitor.is_active,
                "incident_status": inc.status,
                "incident_started_at": inc.started_at,
                "incident_cause": inc.cause,
                "is_inconsistent": False,
                "inconsistency_reason": f"Monitor is {monitor.status}; awaiting checker evaluation",
                "proposed_action": "KEEP_OPEN",
                "resolved_at": None,
                "resolution_reason": None,
            })

    return records


def print_report(records: list[dict], apply_mode: bool):
    sep = "=" * 90
    subsep = "-" * 90

    print(sep)
    print(" RAYTECH MONITOR — INCIDENT RECONCILIATION REPORT")
    print(f" Mode: {'APPLY CHANGES (--fix)' if apply_mode else 'DRY RUN (Read Only, Safe)'}")
    print(f" Timestamp: {utcnow().isoformat()}")
    print(sep)

    inconsistent_records = [r for r in records if r["is_inconsistent"]]
    legitimate_records = [r for r in records if not r["is_inconsistent"]]

    print(f"Total open incidents analyzed: {len(records)}")
    print(f"  - Legitimate active incidents: {len(legitimate_records)}")
    print(f"  - Inconsistent / stale incidents requiring action: {len(inconsistent_records)}")
    print(subsep)

    if not inconsistent_records:
        print("✓ All open incidents are consistent with current monitor states. No action needed.")
        print(sep)
        return

    print("\n[INCONSISTENT INCIDENTS DETECTED]")
    for r in inconsistent_records:
        print(f"\n  • Incident #{r['incident_id']} (Monitor #{r['monitor_id']}: \"{r['monitor_name']}\")")
        print(f"    - Current Monitor Status: {r['monitor_status'].upper()} (Active: {r['monitor_is_active']})")
        print(f"    - Incident Started At:    {r['incident_started_at']}")
        print(f"    - Incident Status:        {r['incident_status'].upper()}")
        print(f"    - Incident Cause:         {r['incident_cause']}")
        print(f"    - Cause of Inconsistency: {r['inconsistency_reason']}")
        print(f"    - Proposed Action:        {r['proposed_action']} (Reason: {r['resolution_reason']})")

    if not apply_mode:
        print("\n" + subsep)
        print(" [SAFETY NOTE]")
        print(" Dry run completed. No modifications were made to the database.")
        print(" To execute reconciliation and resolve these stale incidents safely, run:")
        print("   python -m scripts.reconcile_incidents --fix")
        print(sep)


def apply_reconciliation(db, records: list[dict]) -> int:
    """
    Applies the proposed resolution to inconsistent incidents.
    Does NOT generate email notifications or in-app alerts.
    """
    resolved_count = 0

    for r in records:
        if not r["is_inconsistent"] or r["proposed_action"] == "KEEP_OPEN":
            continue

        incident = db.get(Incident, r["incident_id"])
        if not incident or incident.status != "open":
            continue

        resolved_dt = r["resolved_at"] or utcnow()
        if resolved_dt.tzinfo is None:
            resolved_dt = resolved_dt.replace(tzinfo=timezone.utc)

        started_dt = incident.started_at
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)

        if resolved_dt < started_dt:
            resolved_dt = started_dt

        incident.status = "resolved"
        incident.resolved_at = resolved_dt
        incident.duration_seconds = max(0, int((resolved_dt - started_dt).total_seconds()))
        incident.resolution_reason = r["resolution_reason"]

        resolved_count += 1
        print(f"  [FIXED] Incident #{incident.id} resolved (Reason: {incident.resolution_reason})")

    db.commit()
    return resolved_count


def main():
    parser = argparse.ArgumentParser(description="Reconcile inconsistent monitor incidents.")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply changes to the database. By default, only a dry-run report is generated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform dry run without modifying database (default).",
    )

    args = parser.parse_args()
    apply_mode = args.fix and not args.dry_run

    db = SessionLocal()
    try:
        records = inspect_incidents(db)
        print_report(records, apply_mode)

        if apply_mode:
            print("\nApplying reconciliation changes...")
            count = apply_reconciliation(db, records)
            print(f"\n✓ Successfully reconciled {count} stale incident(s). Zero emails sent.")
            print("=" * 90)

    finally:
        db.close()


if __name__ == "__main__":
    main()
