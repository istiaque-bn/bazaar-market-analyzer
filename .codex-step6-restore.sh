#!/usr/bin/env bash
set -euo pipefail

cd /opt/bazaar

backup_dir="$(find data/backups -mindepth 1 -maxdepth 1 -type d -name '20??????_??????' -print | sort | tail -n 1)"
test -n "$backup_dir"
container_backup="/app/$backup_dir"
restore_db="bazaar_restore_test_20260916"

docker compose --env-file .env.docker exec -T web \
  python manage.py verify_backup "$container_backup"

cleanup() {
  docker exec bazaar-web-1 sh -c \
    'export PGPASSWORD="$POSTGRES_PASSWORD"; dropdb --if-exists -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" "$1"' \
    sh "$restore_db" >/dev/null
}
trap cleanup EXIT

cleanup
docker exec bazaar-web-1 sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; createdb -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" "$1"' \
  sh "$restore_db"
docker exec bazaar-web-1 sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; pg_restore --exit-on-error --no-owner --no-privileges -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$1" "$2"' \
  sh "$restore_db" "$container_backup/db.dump"

expected="$(docker exec bazaar-web-1 python -c \
  'import json,sys; r=json.load(open(sys.argv[1]))["row_counts"]; print("{}|{}|{}".format(r["Stock"], r["PriceHistory"], r["AnalysisResult"]))' \
  "$container_backup/manifest.json")"
actual="$(docker exec bazaar-web-1 sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$1" -At -F "|" -c "SELECT (SELECT count(*) FROM market_stock),(SELECT count(*) FROM market_pricehistory),(SELECT count(*) FROM market_analysisresult);"' \
  sh "$restore_db")"

printf 'backup_dir=%s\n' "$backup_dir"
printf 'expected_counts=%s\n' "$expected"
printf 'restored_counts=%s\n' "$actual"
test "$actual" = "$expected"
printf 'FULL_POSTGRES_RESTORE_VERIFIED\n'
