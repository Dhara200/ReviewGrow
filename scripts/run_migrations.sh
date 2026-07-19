#!/usr/bin/env bash

set -Eeuo pipefail

MYSQL_CONTAINER="${MYSQL_CONTAINER:-reputation_mysql}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-database/migrations}"
BASELINE_MANIFEST_TOOL="${BASELINE_MANIFEST_TOOL:-scripts/migration_baseline.py}"
DATABASE_BASELINE_FROM_INIT_SQL="${DATABASE_BASELINE_FROM_INIT_SQL:-false}"

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

if [ "$DATABASE_BASELINE_FROM_INIT_SQL" = "true" ]; then
  mapfile -t superseded_migrations < <(python "$BASELINE_MANIFEST_TOOL" list)
  ledger_count="$(
    sudo docker exec "$MYSQL_CONTAINER" mysql --batch --skip-column-names \
      -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
      -e "SELECT COUNT(*) FROM schema_migrations;"
  )"

  if [ "$ledger_count" = "0" ]; then
    application_row_count="$(
      sudo docker exec "$MYSQL_CONTAINER" mysql --batch --skip-column-names \
        -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
        -e "SELECT (SELECT COUNT(*) FROM users) + (SELECT COUNT(*) FROM businesses) + (SELECT COUNT(*) FROM reviews);"
    )"
    if [ "$application_row_count" != "0" ]; then
      echo "ERROR: refusing to baseline a database containing application data"
      exit 1
    fi

    for version in "${superseded_migrations[@]}"; do
      validation_sql="$(python "$BASELINE_MANIFEST_TOOL" validation-sql "$version")"
      baseline_valid="$(
        sudo docker exec "$MYSQL_CONTAINER" mysql --batch --skip-column-names \
          -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
          -e "$validation_sql"
      )"
      if [ "$baseline_valid" != "1" ]; then
        echo "ERROR: init.sql schema validation failed for superseded migration: $version"
        exit 1
      fi
      sudo docker exec "$MYSQL_CONTAINER" mysql \
        -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
        -e "INSERT INTO schema_migrations(version) VALUES ('${version}');"
      echo "Recorded init.sql baseline migration: $version"
    done
  else
    for version in "${superseded_migrations[@]}"; do
      recorded="$(
        sudo docker exec "$MYSQL_CONTAINER" mysql --batch --skip-column-names \
          -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" \
          -e "SELECT COUNT(*) FROM schema_migrations WHERE version='${version}';"
      )"
      if [ "$recorded" != "1" ]; then
        echo "ERROR: refusing to rewrite a non-empty migration ledger for: $version"
        exit 1
      fi
    done
  fi
elif [ "$DATABASE_BASELINE_FROM_INIT_SQL" != "false" ]; then
  echo "ERROR: DATABASE_BASELINE_FROM_INIT_SQL must be true or false"
  exit 1
fi

shopt -s nullglob
mapfile -t migration_files < <(printf '%s\n' "$MIGRATIONS_DIR"/*.sql | sort)

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
