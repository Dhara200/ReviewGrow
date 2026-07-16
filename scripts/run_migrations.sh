#!/usr/bin/env bash

set -Eeuo pipefail

MYSQL_CONTAINER="${MYSQL_CONTAINER:-reputation_mysql}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-database/migrations}"

: "${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
: "${MYSQL_USER:?MYSQL_USER is required}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is required}"

echo "Waiting for MySQL..."

for attempt in $(seq 1 30); do
  if sudo docker exec "$MYSQL_CONTAINER" \
    mysqladmin ping \
    -u"$MYSQL_USER" \
    -p"$MYSQL_PASSWORD" \
    --silent; then
    echo "MySQL is ready"
    break
  fi

  if [ "$attempt" -eq 30 ]; then
    echo "ERROR: MySQL did not become ready"
    exit 1
  fi

  sleep 3
done

sudo docker exec -i "$MYSQL_CONTAINER" \
  mysql \
  -u"$MYSQL_USER" \
  -p"$MYSQL_PASSWORD" \
  "$MYSQL_DATABASE" <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  version VARCHAR(255) PRIMARY KEY,
  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
SQL

shopt -s nullglob
migration_files=("$MIGRATIONS_DIR"/*.sql)

for migration_file in "${migration_files[@]}"; do
  version="$(basename "$migration_file")"

  already_applied="$(
    sudo docker exec "$MYSQL_CONTAINER" \
      mysql \
      --batch \
      --skip-column-names \
      -u"$MYSQL_USER" \
      -p"$MYSQL_PASSWORD" \
      "$MYSQL_DATABASE" \
      -e "SELECT COUNT(*) FROM schema_migrations WHERE version='${version}';"
  )"

  if [ "$already_applied" = "1" ]; then
    echo "Skipping already applied migration: $version"
    continue
  fi

  echo "Applying migration: $version"

  sudo docker exec -i "$MYSQL_CONTAINER" \
    mysql \
    --show-warnings \
    -u"$MYSQL_USER" \
    -p"$MYSQL_PASSWORD" \
    "$MYSQL_DATABASE" < "$migration_file"

  sudo docker exec "$MYSQL_CONTAINER" \
    mysql \
    -u"$MYSQL_USER" \
    -p"$MYSQL_PASSWORD" \
    "$MYSQL_DATABASE" \
    -e "INSERT INTO schema_migrations(version) VALUES ('${version}');"

  echo "Applied migration: $version"
done

echo "All pending migrations completed"