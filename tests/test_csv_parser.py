import unittest

import pandas as pd

from app.utils.csv_parser import (
    get_csv_value,
    normalize_csv_row,
)


class CsvParserTests(unittest.TestCase):
    def test_get_csv_value_uses_fallback_for_missing_values(self):
        row = pd.Series({"reviewer_name": pd.NA, "review_text": ""})

        self.assertEqual(get_csv_value(row, ["reviewer_name"], default="Anonymous"), "Anonymous")
        self.assertEqual(get_csv_value(row, ["review_text"], default=""), "")

    def test_normalize_csv_row_returns_clean_values(self):
        row = pd.Series({
            "source": "Google",
            "rating": "5",
            "review_title": "Great",
            "review_text": "Excellent service",
            "reviewer_name": "  John  ",
            "review_date": "20-06-2026",
        })

        normalized = normalize_csv_row(row)

        self.assertEqual(normalized["source"], "Google")
        self.assertEqual(normalized["rating"], 5.0)
        self.assertEqual(normalized["review_title"], "Great")
        self.assertEqual(normalized["review_text"], "Excellent service")
        self.assertEqual(normalized["reviewer_name"], "John")
        self.assertEqual(normalized["review_date"], "2026-06-20")


if __name__ == "__main__":
    unittest.main()
