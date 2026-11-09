#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

BACKUP_DIR=${BACKUP_DIR:-/var/backups/raytech-monitor}
POSTGRES_CONTAINER=${POSTGRES_CONTAINER:-raytech-postgres}
POSTGRES_USER=${POSTGRES_USER:-raytech}
TEMP_DB_PREFIX=${TEMP_DB_PREFIX:-raytech_restore_drill}

log() {
    printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker is required"

case "$TEMP_DB_PREFIX" in
    ''|*[!a-zA-Z0-9_]*) fail "TEMP_DB_PREFIX may contain only letters, numbers, and underscores" ;;
esac

if [ "$#" -gt 1 ]; then
    fail "usage: $0 [archive.dump]"
fi

if [ "$#" -eq 1 ]; then
    archive=$1
else
    shopt -s nullglob
    archives=("$BACKUP_DIR"/raytech_monitor_*.dump)
    shopt -u nullglob
    [ "${#archives[@]}" -gt 0 ] || fail "no backup archives found in $BACKUP_DIR"
    archive=${archives[${#archives[@]} - 1]}
fi

[ -f "$archive" ] || fail "archive does not exist: $archive"
[ -s "$archive" ] || fail "archive is empty: $archive"

suffix="$(date -u +'%Y%m%dT%H%M%SZ')_$$"
temp_db="${TEMP_DB_PREFIX}_${suffix}"
created=0

cleanup() {
    exit_status=$?
    if [ "$created" -eq 1 ]; then
        log "Dropping temporary database: $temp_db"
        docker exec "$POSTGRES_CONTAINER" psql \
            -U "$POSTGRES_USER" \
            -d postgres \
            -v ON_ERROR_STOP=1 \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$temp_db' AND pid <> pg_backend_pid();" \
            >/dev/null 2>&1 || true
        docker exec "$POSTGRES_CONTAINER" dropdb \
            -U "$POSTGRES_USER" \
            --if-exists \
            "$temp_db" \
            >/dev/null 2>&1 || true
    fi
    return "$exit_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

log "Verifying archive catalog: $archive"
docker exec -i "$POSTGRES_CONTAINER" pg_restore -l <"$archive" >/dev/null

log "Creating temporary database: $temp_db"
docker exec "$POSTGRES_CONTAINER" createdb \
    -U "$POSTGRES_USER" \
    --template=template0 \
    "$temp_db"
created=1

log "Restoring archive into temporary database"
docker exec -i "$POSTGRES_CONTAINER" pg_restore \
    -U "$POSTGRES_USER" \
    -d "$temp_db" \
    --exit-on-error \
    --no-owner \
    --no-privileges \
    <"$archive"

core_tables=(users monitors payment_transactions user_subscriptions)
for table in "${core_tables[@]}"; do
    exists=$(docker exec "$POSTGRES_CONTAINER" psql \
        -U "$POSTGRES_USER" \
        -d "$temp_db" \
        -v ON_ERROR_STOP=1 \
        -Atqc "SELECT to_regclass('public.$table') IS NOT NULL;")
    [ "$exists" = "t" ] || fail "required table is missing after restore: $table"

    row_count=$(docker exec "$POSTGRES_CONTAINER" psql \
        -U "$POSTGRES_USER" \
        -d "$temp_db" \
        -v ON_ERROR_STOP=1 \
        -Atqc "SELECT count(*) FROM public.$table;")
    log "Verified table $table with $row_count rows"
done

log "Restore drill passed for archive: $archive"
