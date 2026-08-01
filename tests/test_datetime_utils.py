import unittest
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template_string

from app.utils.datetime_utils import format_datetime_ist, register_datetime_filters


class DateTimeIstTests(unittest.TestCase):
    def test_utc_aware_datetime_is_converted_to_ist(self):
        value = datetime(2026, 8, 1, 9, 29, tzinfo=timezone.utc)
        self.assertEqual("01 Aug 2026, 02:59 PM IST", format_datetime_ist(value))

    def test_naive_datetime_is_treated_as_utc(self):
        value = datetime(2026, 8, 1, 9, 29)
        self.assertEqual("01 Aug 2026, 02:59 PM IST", format_datetime_ist(value))

    def test_none_and_invalid_inputs_use_safe_fallback(self):
        self.assertEqual("-", format_datetime_ist(None))
        self.assertEqual("-", format_datetime_ist("2026-08-01 09:29:00"))

    def test_conversion_handles_indian_date_change(self):
        value = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)
        self.assertEqual("02 Aug 2026, 01:30 AM IST", format_datetime_ist(value))

    def test_aware_non_utc_datetime_uses_declared_offset(self):
        source_timezone = timezone(timedelta(hours=-4))
        value = datetime(2026, 8, 1, 5, 29, tzinfo=source_timezone)
        self.assertEqual("01 Aug 2026, 02:59 PM IST", format_datetime_ist(value))

    def test_jinja_filter_output(self):
        app = Flask(__name__)
        register_datetime_filters(app)
        with app.app_context():
            rendered = render_template_string(
                "{{ created_at | datetime_ist }}",
                created_at=datetime(2026, 8, 1, 9, 29),
            )
        self.assertEqual("01 Aug 2026, 02:59 PM IST", rendered)


if __name__ == "__main__":
    unittest.main()
