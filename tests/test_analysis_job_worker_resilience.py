import unittest
from unittest.mock import MagicMock, patch

import mysql.connector

from app.services.analysis_job_service import process_analysis_job


class AnalysisJobWorkerResilienceTests(unittest.TestCase):
    def _database(self):
        cursor = MagicMock()
        database = MagicMock()
        database.cursor.return_value = cursor
        return database, cursor

    @patch("app.services.analysis_job_service.AIService")
    @patch("app.services.analysis_job_service.get_connection")
    def test_failed_state_persistence_error_is_contained(self, get_connection, _ai):
        database, cursor = self._database()
        get_connection.return_value = database
        cursor.execute.side_effect = [
            RuntimeError("analysis failed"),
            RuntimeError("database unavailable"),
        ]

        result = process_analysis_job(88)

        self.assertFalse(result)
        database.rollback.assert_called()
        cursor.close.assert_called_once()
        database.close.assert_called_once()

    @patch("app.services.analysis_job_service._refresh_job_progress")
    @patch("app.services.analysis_job_service._generate_report", return_value=12)
    @patch("app.services.analysis_job_service.AIService")
    @patch("app.services.analysis_job_service.get_connection")
    def test_completed_state_persistence_error_is_not_converted_to_failed(
        self, get_connection, _ai, _generate_report, _refresh_progress
    ):
        database, cursor = self._database()
        get_connection.return_value = database
        cursor.fetchone.side_effect = [
            {"id": 88, "user_id": 7, "business_id": 9},
            {},
        ]
        cursor.fetchall.return_value = []

        def execute(query, _params=None):
            if "SET status='completed'" in query:
                raise RuntimeError("database unavailable")

        cursor.execute.side_effect = execute

        result = process_analysis_job(88)

        self.assertFalse(result)
        failed_updates = [
            call for call in cursor.execute.call_args_list
            if "SET status='failed'" in call.args[0]
        ]
        self.assertEqual([], failed_updates)
        cursor.close.assert_called_once()
        database.close.assert_called_once()

    @patch("app.services.analysis_job_service.AIService")
    @patch("app.services.analysis_job_service.get_connection")
    def test_mysql_execution_failure_is_left_for_stale_recovery(
        self, get_connection, _ai
    ):
        database, cursor = self._database()
        get_connection.return_value = database
        cursor.execute.side_effect = mysql.connector.OperationalError("server unavailable")

        result = process_analysis_job(88)

        self.assertFalse(result)
        self.assertEqual(1, cursor.execute.call_count)
        cursor.close.assert_called_once()
        database.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
