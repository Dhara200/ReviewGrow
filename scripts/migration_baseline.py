#!/usr/bin/env python3
"""Validate the explicit init.sql migration baseline for deployment scripts."""

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "database" / "migrations"
MANIFEST_PATH = ROOT / "database" / "migration_baseline.json"

AI_COLUMNS = {
    "reviews": (
        "category", "complaint_praise_theme", "suggested_reply",
        "confidence_score", "analysis_error",
    ),
    "analysis_jobs": (
        "id", "user_id", "business_id", "status", "total_reviews",
        "processed_reviews", "failed_reviews", "error_message",
        "force_reanalysis", "latest_report_id", "created_at", "started_at",
        "completed_at",
    ),
    "ai_usage_logs": (
        "id", "user_id", "business_id", "provider", "model_name",
        "operation_type", "input_tokens", "output_tokens", "total_tokens",
        "estimated_cost", "request_status", "response_time_ms",
        "error_message", "created_at",
    ),
    "ai_monthly_usage": (
        "id", "user_id", "business_id", "provider", "model_name",
        "usage_month", "total_requests", "successful_requests",
        "failed_requests", "total_input_tokens", "total_output_tokens",
        "total_tokens", "total_estimated_cost", "average_response_time_ms",
        "updated_at",
    ),
}

AI_INDEXES = {
    "analysis_jobs": (
        "PRIMARY", "idx_analysis_jobs_status", "idx_analysis_jobs_user_id",
        "idx_analysis_jobs_business_id", "idx_analysis_jobs_created_at",
        "idx_analysis_jobs_business_status",
    ),
    "ai_usage_logs": (
        "PRIMARY", "idx_ai_usage_user_id", "idx_ai_usage_business_id",
        "idx_ai_usage_created_at", "idx_ai_usage_provider_model",
        "idx_ai_usage_month",
    ),
    "ai_monthly_usage": (
        "PRIMARY", "uniq_ai_monthly_usage", "idx_ai_monthly_user",
        "idx_ai_monthly_business", "idx_ai_monthly_month",
    ),
}

AI_FOREIGN_KEYS = (
    ("analysis_jobs", "user_id", "users", "CASCADE"),
    ("analysis_jobs", "business_id", "businesses", "CASCADE"),
    ("ai_usage_logs", "user_id", "users", "CASCADE"),
    ("ai_usage_logs", "business_id", "businesses", "SET NULL"),
    ("ai_monthly_usage", "user_id", "users", "CASCADE"),
    ("ai_monthly_usage", "business_id", "businesses", "SET NULL"),
)


def load_manifest(path=MANIFEST_PATH):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("baseline") != "database/init.sql":
        raise ValueError("baseline must be database/init.sql")
    names = manifest.get("superseded_migrations")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("superseded_migrations must be a list of filenames")
    if len(names) != len(set(names)):
        raise ValueError("superseded_migrations contains duplicates")
    reasons = manifest.get("reasons")
    if not isinstance(reasons, dict):
        raise ValueError("reasons must be an object")
    for name in names:
        if Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]+\.sql", name):
            raise ValueError(f"unsafe migration filename: {name!r}")
        if not (MIGRATIONS_DIR / name).is_file():
            raise ValueError(f"superseded migration does not exist: {name}")
        if not isinstance(reasons.get(name), str) or not reasons[name].strip():
            raise ValueError(f"superseded migration has no reason: {name}")
    return manifest


def validation_sql(name):
    if name != "ai_analysis_jobs_migration.sql":
        raise ValueError(f"no schema validator is defined for superseded migration: {name}")
    checks = []
    for table, columns in AI_COLUMNS.items():
        quoted = ",".join(f"'{column}'" for column in columns)
        checks.append(
            "(SELECT COUNT(*) FROM information_schema.columns "
            f"WHERE table_schema=DATABASE() AND table_name='{table}' "
            f"AND column_name IN ({quoted}))={len(columns)}"
        )
    for table, indexes in AI_INDEXES.items():
        quoted = ",".join(f"'{index}'" for index in indexes)
        checks.append(
            "(SELECT COUNT(DISTINCT index_name) FROM information_schema.statistics "
            f"WHERE table_schema=DATABASE() AND table_name='{table}' "
            f"AND index_name IN ({quoted}))={len(indexes)}"
        )
    for table, column, referenced_table, delete_rule in AI_FOREIGN_KEYS:
        checks.append(
            "EXISTS(SELECT 1 FROM information_schema.key_column_usage k "
            "JOIN information_schema.referential_constraints r "
            "ON r.constraint_schema=k.constraint_schema "
            "AND r.constraint_name=k.constraint_name "
            "WHERE k.constraint_schema=DATABASE() "
            f"AND k.table_name='{table}' AND k.column_name='{column}' "
            f"AND k.referenced_table_name='{referenced_table}' "
            f"AND r.delete_rule='{delete_rule}')"
        )
    return "SELECT IF(" + " AND ".join(checks) + ",1,0);"


def main(argv):
    manifest = load_manifest()
    if len(argv) == 1 and argv[0] == "list":
        print("\n".join(manifest["superseded_migrations"]))
        return 0
    if len(argv) == 2 and argv[0] == "validation-sql":
        if argv[1] not in manifest["superseded_migrations"]:
            raise ValueError(f"migration is not superseded by the manifest: {argv[1]}")
        print(validation_sql(argv[1]))
        return 0
    raise ValueError("usage: migration_baseline.py list | validation-sql <filename>")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
