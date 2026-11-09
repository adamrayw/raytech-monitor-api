#!/usr/bin/env bash
set -Eeuo pipefail

API_HEALTH_URL=${API_HEALTH_URL:-https://api.monitor.raytech.cloud/health}
POSTGRES_CONTAINER=${POSTGRES_CONTAINER:-raytech-postgres}
API_CONTAINER=${API_CONTAINER:-raytech-monitor-api}
WORKER_CONTAINER=${WORKER_CONTAINER:-raytech-monitor-worker}
WORKER_HEARTBEAT_FILE=${WORKER_HEARTBEAT_FILE:-/tmp/raytech_worker_heartbeat}
WORKER_HEARTBEAT_MAX_AGE_SECONDS=${WORKER_HEARTBEAT_MAX_AGE_SECONDS:-300}
DISK_PATH=${DISK_PATH:-/}
DISK_WARN_PERCENT=${DISK_WARN_PERCENT:-80}
DISK_CRIT_PERCENT=${DISK_CRIT_PERCENT:-90}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/raytech-monitor}
BACKUP_MAX_AGE_SECONDS=${BACKUP_MAX_AGE_SECONDS:-93600}
POSTGRES_USER=${POSTGRES_USER:-raytech}
POSTGRES_DB=${POSTGRES_DB:-raytech_monitor}
PENDING_STALE_MINUTES=${PENDING_STALE_MINUTES:-30}
STALE_PENDING_MAX_COUNT=${STALE_PENDING_MAX_COUNT:-0}

unhealthy=0
response_file=''

log() {
    printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

mark_unhealthy() {
    log "UNHEALTHY: $*" >&2
    unhealthy=1
}

validate_integer() {
    name=$1
    value=$2
    case "$value" in
        ''|*[!0-9]*)
            log "UNHEALTHY: $name must be a non-negative integer" >&2
            exit 2
            ;;
    esac
}

cleanup() {
    if [ -n "$response_file" ]; then
        rm -f -- "$response_file"
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for item in \
    "DISK_WARN_PERCENT:$DISK_WARN_PERCENT" \
    "DISK_CRIT_PERCENT:$DISK_CRIT_PERCENT" \
    "BACKUP_MAX_AGE_SECONDS:$BACKUP_MAX_AGE_SECONDS" \
    "WORKER_HEARTBEAT_MAX_AGE_SECONDS:$WORKER_HEARTBEAT_MAX_AGE_SECONDS" \
    "PENDING_STALE_MINUTES:$PENDING_STALE_MINUTES" \
    "STALE_PENDING_MAX_COUNT:$STALE_PENDING_MAX_COUNT"; do
    validate_integer "${item%%:*}" "${item#*:}"
done

if [ "$DISK_WARN_PERCENT" -ge "$DISK_CRIT_PERCENT" ] || [ "$DISK_CRIT_PERCENT" -gt 100 ]; then
    log "UNHEALTHY: disk thresholds must satisfy WARN < CRIT <= 100" >&2
    exit 2
fi

case "$API_HEALTH_URL" in
    https://*) ;;
    *)
        log "UNHEALTHY: API_HEALTH_URL must use https://" >&2
        exit 2
        ;;
esac

command -v curl >/dev/null 2>&1 || mark_unhealthy "curl is unavailable"
command -v docker >/dev/null 2>&1 || mark_unhealthy "docker is unavailable"

if command -v curl >/dev/null 2>&1; then
    response_file=$(mktemp)
    if curl --fail --silent --show-error \
        --connect-timeout 5 \
        --max-time 15 \
        "$API_HEALTH_URL" >"$response_file"; then
        if grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' "$response_file"; then
            log "Healthy API response: $API_HEALTH_URL"
        else
            mark_unhealthy "API health response did not report status ok"
        fi
    else
        mark_unhealthy "API health request failed: $API_HEALTH_URL"
    fi
fi

if command -v docker >/dev/null 2>&1; then
    for container in "$POSTGRES_CONTAINER" "$API_CONTAINER" "$WORKER_CONTAINER"; do
        running=$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)
        if [ "$running" = "true" ]; then
            log "Container is running: $container"
        else
            mark_unhealthy "container is not running: $container"
        fi
    done
fi

disk_used=$(df -P "$DISK_PATH" 2>/dev/null | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')
case "$disk_used" in
    ''|*[!0-9]*) mark_unhealthy "could not read disk usage for $DISK_PATH" ;;
    *)
        if [ "$disk_used" -ge "$DISK_CRIT_PERCENT" ]; then
            mark_unhealthy "disk usage is critical at ${disk_used}% on $DISK_PATH"
        elif [ "$disk_used" -ge "$DISK_WARN_PERCENT" ]; then
            mark_unhealthy "disk usage reached warning threshold at ${disk_used}% on $DISK_PATH"
        else
            log "Disk usage is healthy at ${disk_used}% on $DISK_PATH"
        fi
        ;;
esac

shopt -s nullglob
backups=("$BACKUP_DIR"/raytech_monitor_*.dump)
shopt -u nullglob
if [ "${#backups[@]}" -eq 0 ]; then
    mark_unhealthy "no database backup found in $BACKUP_DIR"
else
    latest_backup=${backups[${#backups[@]} - 1]}
    backup_mtime=$(stat -c %Y "$latest_backup" 2>/dev/null || true)
    now=$(date +%s)
    case "$backup_mtime" in
        ''|*[!0-9]*) mark_unhealthy "could not read backup timestamp: $latest_backup" ;;
        *)
            backup_age=$((now - backup_mtime))
            if [ "$backup_age" -lt 0 ] || [ "$backup_age" -gt "$BACKUP_MAX_AGE_SECONDS" ]; then
                mark_unhealthy "latest backup is outside freshness limit: $latest_backup"
            else
                log "Latest backup is fresh: $latest_backup (${backup_age}s old)"
            fi
            ;;
    esac
fi

if command -v docker >/dev/null 2>&1; then
    heartbeat_mtime=$(docker exec "$WORKER_CONTAINER" stat -c %Y "$WORKER_HEARTBEAT_FILE" 2>/dev/null || true)
    now=$(date +%s)
    case "$heartbeat_mtime" in
        ''|*[!0-9]*) mark_unhealthy "worker heartbeat file is missing or unreadable: $WORKER_HEARTBEAT_FILE" ;;
        *)
            heartbeat_age=$((now - heartbeat_mtime))
            if [ "$heartbeat_age" -lt 0 ] || [ "$heartbeat_age" -gt "$WORKER_HEARTBEAT_MAX_AGE_SECONDS" ]; then
                mark_unhealthy "worker heartbeat is stale (${heartbeat_age}s old)"
            else
                log "Worker heartbeat is fresh (${heartbeat_age}s old)"
            fi
            ;;
    esac

    stale_pending=$(docker exec "$POSTGRES_CONTAINER" psql \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        -v ON_ERROR_STOP=1 \
        -Atqc "SELECT count(*) FROM payment_transactions WHERE status = 'PENDING' AND created_at < NOW() - make_interval(mins => $PENDING_STALE_MINUTES);" \
        2>/dev/null || true)
    stale_pending=$(printf '%s' "$stale_pending" | tr -d '[:space:]')
    case "$stale_pending" in
        ''|*[!0-9]*) mark_unhealthy "could not query stale pending payments" ;;
        *)
            if [ "$stale_pending" -gt "$STALE_PENDING_MAX_COUNT" ]; then
                mark_unhealthy "$stale_pending stale pending payments exceed limit $STALE_PENDING_MAX_COUNT"
            else
                log "Stale pending payment count is healthy: $stale_pending"
            fi
            ;;
    esac
fi

if [ "$unhealthy" -ne 0 ]; then
    exit 1
fi

log "All host health checks passed"
