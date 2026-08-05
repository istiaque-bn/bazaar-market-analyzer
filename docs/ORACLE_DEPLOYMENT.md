# Oracle Ampere A1 deployment operations

This runbook supplements `DEPLOYMENT.md` for an Ubuntu host on Oracle
Ampere A1 (`linux/arm64`). It does not authorize deployment or account
changes. Run account-creation and production commands only during an
approved deployment window.

## Architecture and network boundaries

Build the application for `linux/arm64`. The Compose stack exposes only
the Django debugging port on host loopback (`127.0.0.1:8001`). PostgreSQL
and Redis have no host port mappings; Gunicorn is reached by cloudflared
over the private Compose network. The production `cloudflared` service
requires a named-tunnel token from the private Compose environment file.

Redis uses AOF (`appendonly yes`, `appendfsync everysec`) in the
`redis_data` named volume. This reduces queued-task loss across restarts,
but does not eliminate it. Celery tasks must remain idempotent and the
database remains the authoritative record of task outcomes.

## Local database backups

The application image includes PostgreSQL 16 `pg_dump` and `pg_restore`,
matching the Compose database server major version. The default command
writes into the bind-mounted persistent directory
`/app/data/backups`:

```bash
docker compose --env-file .env.docker exec -T web \
  python manage.py backup_bazaar
```

`--dest` remains available for an operator-selected destination. Always
pass a specific backup directory to verification:

```bash
docker compose --env-file .env.docker exec -T web \
  python manage.py verify_backup /app/data/backups/<timestamp>
```

## Encrypted Google Drive backups with rclone

Google Drive is off-site backup storage only. Never mount it as the live
PostgreSQL data directory, Redis directory, Django data directory, or an
active database filesystem.

### Install rclone on Ubuntu

Prefer Ubuntu's signed package repository and record the installed version:

```bash
sudo apt-get update
sudo apt-get install --yes rclone
rclone version
```

Keep rclone's configuration readable only by the deployment account:

```bash
mkdir -p "$HOME/.config/rclone"
chmod 700 "$HOME/.config/rclone"
```

### Configure a Drive remote and an encrypted remote

Run `rclone config` interactively as the deployment account:

1. Create a Google Drive remote named `gdrive`. Complete OAuth in the
   operator-controlled browser. Do not paste OAuth material into this
   repository or a shell transcript.
2. Create a second remote named `gdrive-crypt` with storage type `crypt`.
3. Set its upstream path to a dedicated directory such as
   `gdrive:BazaarEncrypted`.
4. Enable filename encryption and directory-name encryption.
5. Generate and store the crypt password and salt in an approved password
   manager. Never put them in `.env.docker`, Git, unit files, or this
   runbook.
6. Confirm permissions without printing configuration secrets:

```bash
chmod 600 "$HOME/.config/rclone/rclone.conf"
rclone lsd gdrive-crypt:
```

### Required daily sequence

Every run must perform these operations in order. Do not use `rclone
sync`; it can propagate deletions. No automatic local or remote deletion
is configured in this patch.

```bash
set -euo pipefail
cd /opt/bazaar

docker compose --env-file .env.docker exec -T web \
  python manage.py backup_bazaar

backup_dir="$(find data/backups -mindepth 1 -maxdepth 1 -type d -print | sort | tail -n 1)"
test -n "$backup_dir"

docker compose --env-file .env.docker exec -T web \
  python manage.py verify_backup "/app/$backup_dir"

backup_name="$(basename "$backup_dir")"
rclone copy "$backup_dir" "gdrive-crypt:bazaar/$backup_name" --checkers 4 --transfers 2
rclone check "$backup_dir" "gdrive-crypt:bazaar/$backup_name" --one-way
logger --tag bazaar-backup "verified encrypted backup upload succeeded: $backup_name"
```

The success record must occur only after `rclone check` exits successfully.
Operational logging must contain the timestamped backup name, never OAuth
tokens, crypt passwords, database passwords, or rclone configuration.

### Daily schedule at 02:30 Asia/Dhaka

Place the reviewed sequence in a root-owned executable such as
`/usr/local/sbin/bazaar-backup-to-drive` with mode `0750`, then use a
systemd timer. Systemd's calendar syntax records the timezone explicitly:

```ini
# /etc/systemd/system/bazaar-backup.service
[Unit]
Description=Verify and upload Bazaar backup

[Service]
Type=oneshot
User=bazaar
WorkingDirectory=/opt/bazaar
ExecStart=/usr/local/sbin/bazaar-backup-to-drive
```

```ini
# /etc/systemd/system/bazaar-backup.timer
[Unit]
Description=Run Bazaar backup at 02:30 Asia/Dhaka

[Timer]
OnCalendar=*-*-* 02:30:00 Asia/Dhaka
Persistent=true
Unit=bazaar-backup.service

[Install]
WantedBy=timers.target
```

Before enabling it, review with `systemd-analyze calendar '*-*-* 02:30:00
Asia/Dhaka'`. Enabling the timer is a production action and is deliberately
not performed by this repository patch.

### Isolated download and restore test

Never restore over the live database. Choose one known uploaded timestamp,
download it into a dedicated scratch path, and verify it first:

```bash
cd /opt/bazaar
restore_name=<timestamp>
restore_dir="data/restore-tests/$restore_name"
mkdir -p "$restore_dir"
rclone copy "gdrive-crypt:bazaar/$restore_name" "$restore_dir"
rclone check "$restore_dir" "gdrive-crypt:bazaar/$restore_name" --one-way
docker compose --env-file .env.docker exec -T web \
  python manage.py verify_backup "/app/$restore_dir"
```

For a PostgreSQL backup, `verify_backup` validates checksums and the dump
catalog. A full drill additionally requires a separately named disposable
PostgreSQL database. Create that database, restore `db.dump` into it with
`pg_restore`, compare recorded model row counts and spot-check values, then
drop it only after confirming the target name is the disposable test
database. Never use the production database name for this drill.

Keep the downloaded files until the drill result is recorded. Their later
removal and any backup-retention policy are separate, explicitly approved
operations.
