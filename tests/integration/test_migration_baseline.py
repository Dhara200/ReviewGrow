import json
import tempfile
import unittest
from pathlib import Path

from scripts.migration_baseline import load_manifest, validation_sql
from tests.integration.test_google_review_sync_mysql_e2e import (
    BASELINE_MANIFEST,
    _load_baseline_manifest,
)


class MigrationBaselineManifestTests(unittest.TestCase):
    def write_manifest(self, payload):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "baseline.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def valid_payload(self):
        return json.loads(BASELINE_MANIFEST.read_text(encoding="utf-8"))

    def test_repository_manifest_is_valid(self):
        manifest = _load_baseline_manifest()
        self.assertEqual(
            ["ai_analysis_jobs_migration.sql"],
            manifest["superseded_migrations"],
        )
        self.assertEqual(manifest, load_manifest())
        sql = validation_sql("ai_analysis_jobs_migration.sql")
        for expected_object in (
            "analysis_jobs", "ai_usage_logs", "ai_monthly_usage",
            "idx_analysis_jobs_business_status", "uniq_ai_monthly_usage",
        ):
            self.assertIn(expected_object, sql)

    def test_duplicate_filename_is_rejected(self):
        payload = self.valid_payload()
        payload["superseded_migrations"] *= 2
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            _load_baseline_manifest(self.write_manifest(payload))

    def test_missing_or_renamed_migration_is_rejected(self):
        payload = self.valid_payload()
        payload["superseded_migrations"] = ["renamed_migration.sql"]
        payload["reasons"] = {"renamed_migration.sql": "test"}
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            _load_baseline_manifest(self.write_manifest(payload))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_manifest(self.write_manifest(payload))

    def test_path_components_are_rejected(self):
        for unsafe in ("../ai_analysis_jobs_migration.sql", "folder/migration.sql", "folder\\migration.sql"):
            with self.subTest(unsafe=unsafe):
                payload = self.valid_payload()
                payload["superseded_migrations"] = [unsafe]
                payload["reasons"] = {unsafe: "test"}
                with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                    _load_baseline_manifest(self.write_manifest(payload))
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    load_manifest(self.write_manifest(payload))

    def test_unknown_superseded_migration_has_no_schema_validator(self):
        with self.assertRaisesRegex(ValueError, "no schema validator"):
            validation_sql("unknown.sql")

    def test_production_runner_requires_explicit_baseline_mode(self):
        runner = (
            Path(__file__).resolve().parents[2] / "scripts" / "run_migrations.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('DATABASE_BASELINE_FROM_INIT_SQL="${DATABASE_BASELINE_FROM_INIT_SQL:-false}"', runner)
        self.assertIn('if [ "$DATABASE_BASELINE_FROM_INIT_SQL" = "true" ]', runner)
        self.assertIn("refusing to baseline a database containing application data", runner)
        self.assertIn("refusing to rewrite a non-empty migration ledger", runner)
        self.assertIn('python "$BASELINE_MANIFEST_TOOL" validation-sql', runner)


if __name__ == "__main__":
    unittest.main()
