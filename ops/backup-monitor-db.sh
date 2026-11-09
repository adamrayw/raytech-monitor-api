#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

BACKUP_DIR=${BACKUP_DIR:-/var/backups/raytech-monitor}
RETENTION_DAYS=${RETENTION_DAYS:-14}
POSTGRES_CONTAINER=${POSTGRES_CONTAINER:-raytech-postgres}
POSTGRES_USER=${POSTGRES_USER:-raytech}
POSTGRES_DB=${POSTGRES_DB:-raytech_monitor}
UPLOAD_S3=${UPLOAD_S3:-0}
S3_BUCKET=${S3_BUCKET:-}
S3_PREFIX=${S3_PREFIX:-raytech-monitor}
S3_ENDPOINT_URL=${S3_ENDPOINT_URL:-}
S3_REGION=${S3_REGION:-}

log() {
    printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

case "$RETENTION_DAYS" in
    ''|*[!0-9]*) fail "RETENTION_DAYS must be a non-negative integer" ;;
esac

case "$UPLOAD_S3" in
    0|1) ;;
    *) fail "UPLOAD_S3 must be 0 or 1" ;;
esac

command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v flock >/dev/null 2>&1 || fail "flock is required"
command -v install >/dev/null 2>&1 || fail "install is required"

install -d -m 0700 "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

lock_file="$BACKUP_DIR/.backup.lock"
exec 9>"$lock_file"
if ! flock -n 9; then
    fail "another backup is already running"
fi

timestamp=$(date -u +'%Y%m%dT%H%M%SZ')
backup_name="raytech_monitor_${timestamp}.dump"
backup_path="$BACKUP_DIR/$backup_name"
[ ! -e "$backup_path" ] || fail "backup already exists: $backup_path"

temp_path=$(mktemp "$BACKUP_DIR/.${backup_name}.tmp.XXXXXX")
cleanup() {
    rm -f -- "$temp_path"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

log "Starting PostgreSQL backup to temporary archive"
if ! docker exec "$POSTGRES_CONTAINER" pg_dump \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    -Fc >"$temp_path"; then
    fail "pg_dump failed"
fi

[ -s "$temp_path" ] || fail "pg_dump produced an empty archive"

log "Verifying archive catalog with pg_restore"
if ! docker exec -i "$POSTGRES_CONTAINER" pg_restore -l <"$temp_path" >/dev/null; then
    fail "pg_restore could not read the archive catalog"
fi

chmod 0600 "$temp_path"
mv -- "$temp_path" "$backup_path"
log "Backup completed: $backup_path"

if [ "$UPLOAD_S3" = "1" ]; then
    [ -n "$S3_BUCKET" ] || fail "S3_BUCKET is required when UPLOAD_S3=1"
    command -v aws >/dev/null 2>&1 || fail "aws CLI is required when UPLOAD_S3=1"

    prefix=${S3_PREFIX#/}
    prefix=${prefix%/}
    if [ -n "$prefix" ]; then
        destination="s3://$S3_BUCKET/$prefix/$backup_name"
    else
        destination="s3://$S3_BUCKET/$backup_name"
    fi

    aws_args=(s3 cp "$backup_path" "$destination" --only-show-errors)
    if [ -n "$S3_ENDPOINT_URL" ]; then
        aws_args+=(--endpoint-url "$S3_ENDPOINT_URL")
    fi
    if [ -n "$S3_REGION" ]; then
        aws_args+=(--region "$S3_REGION")
    fi

    log "Uploading backup to configured object storage destination"
    aws "${aws_args[@]}"
    log "Object storage upload completed"
fi

log "Removing archives older than $RETENTION_DAYS days"
find "$BACKUP_DIR" \
    -type f \
    -name 'raytech_monitor_*.dump' \
    ! -name "$backup_name" \
    -mmin "+$((RETENTION_DAYS * 1440))" \
    -delete

log "Backup retention completed"
