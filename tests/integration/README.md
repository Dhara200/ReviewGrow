# MySQL integration tests

## Atomic login limiter suite

The limiter suite never creates or drops a database. Create a disposable local
database and least-privileged test user explicitly. The name must end in
`_test` or `_testing` and must not resemble an application database.

Example as a local MySQL administrator (replace both passwords):

```sql
CREATE DATABASE reviewgrow_limiter_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'reviewgrow_limiter_test'@'%' IDENTIFIED BY '<disposable-test-password>';
GRANT ALL PRIVILEGES ON reviewgrow_limiter_test.* TO 'reviewgrow_limiter_test'@'%';
```

Then opt in explicitly from PowerShell:

```powershell
$env:TEST_MYSQL_HOST = "127.0.0.1"
$env:TEST_MYSQL_PORT = "3306"
$env:TEST_MYSQL_USER = "reviewgrow_limiter_test"
$env:TEST_MYSQL_PASSWORD = "<disposable-test-password>"
$env:TEST_MYSQL_DATABASE = "reviewgrow_limiter_test"
python -m unittest tests.integration.test_limiter_mysql -v
```

The suite prints only the validated database name, applies the real
`20260722_001_create_rate_limit_counters.sql` migration, and creates, truncates,
and drops only `rate_limit_counters` within that configured database. It never
prints credentials and never falls back to the normal application database.

Cleanup, performed separately by a local administrator after testing:

```sql
DROP DATABASE reviewgrow_limiter_test;
DROP USER 'reviewgrow_limiter_test'@'%';
```

## Google review sync suite

These tests require the local MySQL 8 service and an isolated database whose
name ends in `_test`. They never fall back to an in-memory repository.

PowerShell example:

```powershell
$env:TEST_MYSQL_HOST = "127.0.0.1"
$env:TEST_MYSQL_PORT = "3306"
$env:TEST_MYSQL_USER = "root"
$env:TEST_MYSQL_PASSWORD = "<local Docker root password>"
$env:TEST_MYSQL_DATABASE = "reviewgrow_integration_test"
python -m unittest tests.integration.test_google_review_sync_mysql_e2e -v
```

The configured user must be allowed to create and drop the dedicated test
database. The suite applies `database/init.sql`, validates the explicit
`database/migration_baseline.json` manifest against `INFORMATION_SCHEMA`, and
then applies every non-superseded SQL file in `database/migrations/`, ordered by
filename. Database-selection directives are treated as runner metadata and
execution remains pinned to `TEST_MYSQL_DATABASE`; migration SQL is not
rewritten.

Run the manifest checks without MySQL:

```powershell
python -m unittest tests.integration.test_migration_baseline -v
```

For a brand-new production database created from the current `init.sql`, seed
the explicit baseline and then run all non-superseded migrations with:

```bash
DATABASE_BASELINE_FROM_INIT_SQL=true ./scripts/run_migrations.sh
```

Use that flag only immediately after `init.sql` on an empty fresh database. It
defaults to `false`. The runner validates the baseline schema, records the
manifest's superseded migrations in `schema_migrations`, and then uses that
ledger normally. It refuses to seed a database containing application data or
to rewrite a non-empty ledger missing a baseline entry. Subsequent runs may use
the normal command:

```bash
./scripts/run_migrations.sh
```
