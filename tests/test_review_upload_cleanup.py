import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from flask import Flask

from app.routes.reviews import review_bp


class ReviewUploadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="upload-cleanup-test")
        self.app.register_blueprint(review_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 7

    def _connection(self):
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor
        return connection, cursor

    def _post(self):
        response = self.client.post(
            "/reviews/upload-ui",
            data={
                "business_id": "9",
                "file": (io.BytesIO(b"temporary spreadsheet"), "reviews.xlsx"),
            },
            content_type="multipart/form-data",
        )
        response.close()
        return response

    def _base_patches(self, upload_path):
        return (
            patch("app.services.subscription_service.has_active_subscription", return_value=True),
            patch("app.routes.reviews.user_owns_business", return_value=True),
            patch("app.routes.reviews._safe_upload_path", return_value=upload_path),
        )

    def test_uploaded_file_is_deleted_after_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = os.path.join(temp_dir, "upload.xlsx")
            connection, _ = self._connection()
            dataframe = pd.DataFrame([{"review_text": "Excellent", "rating": 5}])
            subscription, ownership, safe_path = self._base_patches(upload_path)

            with subscription, ownership, safe_path, patch(
                "app.routes.reviews.read_reviews_file", return_value=dataframe
            ), patch(
                "app.routes.reviews.get_connection", return_value=connection
            ), patch(
                "app.routes.reviews.create_analysis_job", return_value=(41, True)
            ):
                response = self._post()

            self.assertEqual(response.status_code, 302)
            self.assertFalse(os.path.exists(upload_path))

    def test_uploaded_file_is_deleted_after_each_processing_failure(self):
        failure_stages = ("parser", "connection", "insert", "commit", "analysis")

        for stage in failure_stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp_dir:
                upload_path = os.path.join(temp_dir, "upload.xlsx")
                connection, cursor = self._connection()
                dataframe = pd.DataFrame([{"review_text": "Excellent", "rating": 5}])
                subscription, ownership, safe_path = self._base_patches(upload_path)

                if stage == "insert":
                    cursor.execute.side_effect = RuntimeError("insert failed")
                if stage == "commit":
                    connection.commit.side_effect = RuntimeError("commit failed")

                parser_result = (
                    RuntimeError("parser failed") if stage == "parser" else dataframe
                )
                database_result = (
                    RuntimeError("connection failed") if stage == "connection" else connection
                )
                analysis_result = (
                    RuntimeError("analysis failed") if stage == "analysis" else (41, True)
                )

                with subscription, ownership, safe_path, patch(
                    "app.routes.reviews.read_reviews_file",
                    side_effect=parser_result if isinstance(parser_result, Exception) else None,
                    return_value=None if isinstance(parser_result, Exception) else parser_result,
                ), patch(
                    "app.routes.reviews.get_connection",
                    side_effect=database_result if isinstance(database_result, Exception) else None,
                    return_value=None if isinstance(database_result, Exception) else database_result,
                ), patch(
                    "app.routes.reviews.create_analysis_job",
                    side_effect=analysis_result if isinstance(analysis_result, Exception) else None,
                    return_value=None if isinstance(analysis_result, Exception) else analysis_result,
                ):
                    response = self._post()

                self.assertEqual(response.status_code, 302)
                self.assertFalse(os.path.exists(upload_path))


if __name__ == "__main__":
    unittest.main()
