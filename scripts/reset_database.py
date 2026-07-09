"""
Safely reset the application database for testing while preserving one admin user.

This project uses mysql-connector through app.services.database_service.get_connection,
so the script reuses the existing Flask app database configuration.
"""

from __future__ import annotations

import sys
from pathlib import Path


ADMIN_EMAIL = "dharaprasath52@gmail.com"


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import Config  # noqa: E402
from app.services.database_service import get_connection  # noqa: E402


USERS_TABLE = "users"


def quote_identifier(name: str) -> str:
    return f"`{name.replace('`', '``')}`"


def fetch_tables(cursor) -> list[str]:
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema=%s
        AND table_type='BASE TABLE'
        ORDER BY table_name
        """,
        (Config.DB_NAME,),
    )
    return [row["table_name"] for row in cursor.fetchall()]


def table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.tables
        WHERE table_schema=%s
        AND table_name=%s
        AND table_type='BASE TABLE'
        """,
        (Config.DB_NAME, table_name),
    )
    return int((cursor.fetchone() or {}).get("table_count") or 0) > 0


def table_has_auto_increment(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS column_count
        FROM information_schema.columns
        WHERE table_schema=%s
        AND table_name=%s
        AND extra LIKE '%%auto_increment%%'
        """,
        (Config.DB_NAME, table_name),
    )
    return int((cursor.fetchone() or {}).get("column_count") or 0) > 0


def count_rows(cursor, table_name: str) -> int:
    cursor.execute(f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table_name)}")
    return int((cursor.fetchone() or {}).get("row_count") or 0)


def count_where(cursor, table_name: str, where_sql: str, params: tuple = ()) -> int:
    if not table_exists(cursor, table_name):
        return 0
    cursor.execute(
        f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table_name)} WHERE {where_sql}",
        params,
    )
    return int((cursor.fetchone() or {}).get("row_count") or 0)


def abort_if_admin_missing(cursor) -> int:
    if not table_exists(cursor, USERS_TABLE):
        raise RuntimeError("Abort: users table does not exist.")

    cursor.execute(
        """
        SELECT id
        FROM users
        WHERE email=%s
        LIMIT 1
        """,
        (ADMIN_EMAIL,),
    )
    admin = cursor.fetchone()
    if not admin:
        raise RuntimeError(
            f"Abort: protected admin user was not found: {ADMIN_EMAIL}"
        )
    return int(admin["id"])


def delete_application_data(cursor, tables: list[str]) -> list[str]:
    emptied_tables = []

    cursor.execute("SET FOREIGN_KEY_CHECKS=0")

    for table_name in tables:
        if table_name == USERS_TABLE:
            continue
        print(f"Deleting {table_name}...")
        cursor.execute(f"DELETE FROM {quote_identifier(table_name)}")
        emptied_tables.append(table_name)

    print("Deleting all non-admin users...")
    cursor.execute(
        """
        DELETE FROM users
        WHERE email <> %s
        OR email IS NULL
        """,
        (ADMIN_EMAIL,),
    )

    return emptied_tables


def reset_auto_increments(cursor, tables: list[str]) -> None:
    print("Resetting auto increments...")
    for table_name in tables:
        if count_rows(cursor, table_name) != 0:
            continue
        if not table_has_auto_increment(cursor, table_name):
            continue
        print(f"Resetting {table_name} AUTO_INCREMENT...")
        cursor.execute(f"ALTER TABLE {quote_identifier(table_name)} AUTO_INCREMENT = 1")


def print_final_verification(cursor) -> None:
    users_remaining = count_rows(cursor, USERS_TABLE) if table_exists(cursor, USERS_TABLE) else 0
    businesses = count_rows(cursor, "businesses") if table_exists(cursor, "businesses") else 0
    google_reviews = count_where(cursor, "reviews", "source='google'")
    excel_reviews = count_where(cursor, "reviews", "source='excel'")
    reports = count_rows(cursor, "reports") if table_exists(cursor, "reports") else 0
    ai_reports = (
        count_rows(cursor, "ai_consultant_reports")
        if table_exists(cursor, "ai_consultant_reports")
        else 0
    )
    consultant_actions = (
        count_rows(cursor, "consultant_actions")
        if table_exists(cursor, "consultant_actions")
        else 0
    )

    print("")
    print(f"Users remaining: {users_remaining}")
    print(f"Businesses: {businesses}")
    print(f"Google reviews: {google_reviews}")
    print(f"Excel reviews: {excel_reviews}")
    print(f"Reports: {reports}")
    print(f"AI Consultant reports: {ai_reports}")
    print(f"Consultant actions: {consultant_actions}")

    if (
        users_remaining == 1
        and businesses == 0
        and google_reviews == 0
        and excel_reviews == 0
        and reports == 0
        and ai_reports == 0
        and consultant_actions == 0
    ):
        print("Database successfully reset.")
    else:
        print("Database reset completed, but verification counts need review.")


def reset_database() -> None:
    print("Starting database reset...")
    print(f"Protected admin email: {ADMIN_EMAIL}")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        admin_id = abort_if_admin_missing(cursor)
        print(f"Protected admin user found: id={admin_id}")

        tables = fetch_tables(cursor)
        emptied_tables = delete_application_data(cursor, tables)

        conn.commit()

        reset_auto_increments(cursor, emptied_tables)
        conn.commit()

        print_final_verification(cursor)
    except Exception:
        conn.rollback()
        print("Database reset failed. Rolled back pending changes.")
        raise
    finally:
        try:
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            conn.commit()
        finally:
            cursor.close()
            conn.close()


if __name__ == "__main__":
    reset_database()
