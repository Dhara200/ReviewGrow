import os
import tempfile
import unittest

import pandas as pd

from app.utils.excel_parser import read_reviews_file


class ExcelParserTests(unittest.TestCase):
    def test_read_reviews_file_loads_xlsx_reviews(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = os.path.join(temp_dir, "reviews.xlsx")
            pd.DataFrame(
                [
                    {
                        "source": "Google",
                        "rating": 5,
                        "review_text": "Excellent service",
                        "reviewer_name": "John",
                        "review_date": "20-06-2026",
                    }
                ]
            ).to_excel(upload_path, index=False)

            df = read_reviews_file(upload_path)

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["review_text"], "Excellent service")
        self.assertEqual(df.iloc[0]["rating"], 5)

    def test_read_reviews_file_rejects_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_path = os.path.join(temp_dir, "reviews.csv")
            pd.DataFrame([{"review_text": "Good"}]).to_csv(upload_path, index=False)

            with self.assertRaisesRegex(ValueError, "Excel file"):
                read_reviews_file(upload_path)


if __name__ == "__main__":
    unittest.main()
