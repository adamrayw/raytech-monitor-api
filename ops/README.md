# Raytech Monitor production operations

These files provide database backup, restore-drill, and host-health checks for a Docker-based host. They do not deploy or modify the application.

## Requirements

- Bash 4 or newer
- Docker CLI access to `raytech-postgres`, `raytech-monitor-api`, and `raytech-monitor-worker`
- GNU `date`, `find`, `stat`, `df`, `awk`, `grep`, `flock`, and `install`
- `curl` for host health checks
- AWS CLI only when S3-compatible upload is enabled
- PostgreSQL client programs inside `raytech-postgres`

Run scripts as an account allowed to use Docker. The supplied systemd service runs as root because Docker socket access and `/var/backups` commonly require it.

## Manual installation

From `raytech-monitor-api`:

```sh
sudo install -d -m 0755 /usr/local/sbin
sudo install -m 0750 ops/backup-monitor-db.sh /usr/local/sbin/backup-monitor-db
sudo install -m 0750 ops/restore-drill.sh /usr/local/sbin/raytech-monitor-restore-drill
sudo install -m 0750 ops/host-health.sh /usr/local/sbin/raytech-monitor-host-health
sudo install -d -m 0700 /var/backups/raytech-monitor
sudo install -d -m 0750 /etc/raytech-monitor
sudo install -m 0644 ops/raytech-monitor-backup.service /etc/systemd/system/raytech-monitor-backup.service
sudo install -m 0644 ops/raytech-monitor-backup.timer /etc/systemd/system/raytech-monitor-backup.timer
sudo systemctl daemon-reload
```

Create `/etc/raytech-monitor/backup.env` only when defaults need changing. Keep it owned by root with mode `0600`. Do not put database passwords in this file; the scripts invoke PostgreSQL tools inside the existing database container.

## Enable the backup timer

The timer runs daily at 02:30 in `Asia/Jakarta` and catches a missed run after the host starts.

```sh
sudo systemctl enable --now raytech-monitor-backup.timer
systemctl list-timers raytech-monitor-backup.timer
```

## Run and inspect a backup

```sh
sudo systemctl start raytech-monitor-backup.service
sudo systemctl status raytech-monitor-backup.service
sudo journalctl -u raytech-monitor-backup.service --since today
sudo /usr/local/sbin/backup-monitor-db
```

Backups are written through a restrictive temporary file, checked for non-empty content, verified with `pg_restore -l`, and then renamed to `raytech_monitor_YYYYMMDDTHHMMSSZ.dump`. Retention runs only after a valid archive has been published and excludes that current archive.

## Run a restore drill

With no argument, the script restores the newest local archive. A path selects a specific archive.

```sh
sudo /usr/local/sbin/raytech-monitor-restore-drill
sudo /usr/local/sbin/raytech-monitor-restore-drill /var/backups/raytech-monitor/raytech_monitor_YYYYMMDDTHHMMSSZ.dump
```

The drill creates a uniquely named temporary database, restores the archive, checks `users`, `monitors`, `payment_transactions`, and `user_subscriptions`, and drops only that temporary database through an exit trap.

## Run host health checks

```sh
sudo /usr/local/sbin/raytech-monitor-host-health
printf 'exit status: %s\n' "$?"
```

Exit status `0` means every check passed. Status `1` means one or more runtime checks failed. Status `2` means configuration is invalid.

## Environment variables

### Backup

- `BACKUP_DIR` (default `/var/backups/raytech-monitor`)
- `RETENTION_DAYS` (default `14`)
- `POSTGRES_CONTAINER` (default `raytech-postgres`)
- `POSTGRES_USER` (default `raytech`)
- `POSTGRES_DB` (default `raytech_monitor`)

### Optional S3 or R2 upload

Uploads are disabled unless `UPLOAD_S3=1` is set. When enabled, `S3_BUCKET` is required and AWS CLI credentials must already be available to the service account through its normal credential chain.

- `UPLOAD_S3` (default `0`)
- `S3_BUCKET`
- `S3_PREFIX` (default `raytech-monitor`)
- `S3_ENDPOINT_URL` (set this to the S3-compatible endpoint for R2)
- `S3_REGION`

Credential names used by AWS CLI may include `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`. Store values outside this repository. Do not pass them on the command line or write them to logs.

Example configuration names, without values:

```sh
UPLOAD_S3=1
S3_BUCKET=
S3_PREFIX=raytech-monitor
S3_ENDPOINT_URL=
S3_REGION=
```

### Restore drill

- `BACKUP_DIR` (default `/var/backups/raytech-monitor`)
- `POSTGRES_CONTAINER` (default `raytech-postgres`)
- `POSTGRES_USER` (default `raytech`)
- `TEMP_DB_PREFIX` (default `raytech_restore_drill`)

### Host health

- `API_HEALTH_URL` (default `https://api.monitor.raytech.cloud/health`; HTTPS is required)
- `POSTGRES_CONTAINER` (default `raytech-postgres`)
- `API_CONTAINER` (default `raytech-monitor-api`)
- `WORKER_CONTAINER` (default `raytech-monitor-worker`)
- `WORKER_HEARTBEAT_FILE` (default `/tmp/raytech_worker_heartbeat`)
- `WORKER_HEARTBEAT_MAX_AGE_SECONDS` (default `300`)
- `DISK_PATH` (default `/`)
- `DISK_WARN_PERCENT` (default `80`)
- `DISK_CRIT_PERCENT` (default `90`)
- `BACKUP_DIR` (default `/var/backups/raytech-monitor`)
- `BACKUP_MAX_AGE_SECONDS` (default `93600`, or 26 hours)
- `POSTGRES_USER` (default `raytech`)
- `POSTGRES_DB` (default `raytech_monitor`)
- `PENDING_STALE_MINUTES` (default `30`)
- `STALE_PENDING_MAX_COUNT` (default `0`)

## Disable the timer

```sh
sudo systemctl disable --now raytech-monitor-backup.timer
```
