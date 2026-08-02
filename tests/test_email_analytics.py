import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from flask import Flask

from app.routes.admin import admin_bp
from app.services.email_analytics_service import EmailAnalyticsService, normalize_filters
from app.utils.datetime_utils import format_datetime_ist


class Args(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        if type and value not in (None, ""):
            try: return type(value)
            except (TypeError, ValueError): return default
        return value


class Cursor:
    def __init__(self, one=None, all_rows=None):
        self.one = one or {"total": 0}; self.all_rows = all_rows or []
        self.executions=[]; self.closed=False
    def execute(self, sql, params=()): self.executions.append((" ".join(sql.split()), params))
    def fetchone(self): return self.one
    def fetchall(self): return self.all_rows
    def close(self): self.closed=True


class Connection:
    def __init__(self, cursor): self.value=cursor; self.closed=False
    def cursor(self, dictionary=False): return self.value
    def close(self): self.closed=True


class EmailAnalyticsServiceTests(unittest.TestCase):
    def test_filters_are_allowlisted_bounded_and_paginated(self):
        result=normalize_filters(Args({"status":"hacked","email_type":"login_otp","page":"-4","per_page":"999","sort":"DROP"}))
        self.assertEqual("",result["status"]); self.assertEqual("login_otp",result["email_type"])
        self.assertEqual(1,result["page"]); self.assertEqual(100,result["per_page"]); self.assertEqual("newest",result["sort"])

    def test_paginated_query_is_parameterized_and_redacts_otp_payload(self):
        cursor=Cursor(one={"total":1},all_rows=[{
            "id":1,"customer_name":"Owner","recipient_email":"o@example.com",
            "business_name":"Shop","email_type":"login_otp","template_name":"login_otp",
            "priority":10,"status":"sent","created_at":datetime(2026,8,2),
            "processing_started_at":None,"sent_at":None,"processing_seconds":None,
            "queue_delay_seconds":1,"attempt_count":1,"max_attempts":6,
            "ses_message_id":"ses-1","last_error":None,"deduplication_key":"login_otp:1",
            "template_data":'{"otp_code":"123456","secret":"bad","plan_name":"Premium"}',
            "subscription_plan":"starter",
        }])
        service=EmailAnalyticsService(lambda:Connection(cursor))
        filters=normalize_filters(Args({"q":"Owner","page":"1","per_page":"25"}))
        rows,total=service._jobs(cursor,filters)
        self.assertEqual(1,total); self.assertNotIn("template_data",rows[0])
        self.assertNotIn("otp_code",rows[0]["safe_template_variables"])
        self.assertNotIn("secret",rows[0]["safe_template_variables"])
        self.assertIn("LIKE %s",cursor.executions[0][0]); self.assertIn("LIMIT %s OFFSET %s",cursor.executions[1][0])

    def test_chart_queries_return_serializable_series(self):
        class ChartCursor(Cursor):
            def __init__(self): super().__init__(); self.calls=0
            def fetchall(self):
                self.calls+=1
                return [{"label":datetime(2026,8,2).date() if self.calls==1 else "sent","value":2}]
        charts=EmailAnalyticsService()._charts(ChartCursor())
        self.assertEqual("2026-08-02",charts["daily"][0]["label"])
        self.assertEqual(2,charts["statuses"][0]["value"])


class EmailAnalyticsRouteTests(unittest.TestCase):
    def setUp(self):
        app=Flask(__name__,template_folder="../app/templates"); app.secret_key="test"
        app.config.update(TESTING=True); app.register_blueprint(admin_bp)
        app.jinja_env.filters["datetime_ist"]=format_datetime_ist
        app.jinja_env.globals.update(csrf_field=lambda:"",csrf_token=lambda:"")
        self.client=app.test_client()

    def test_anonymous_redirected_and_non_admin_forbidden(self):
        self.assertEqual(302,self.client.get("/admin/email-analytics").status_code)
        with self.client.session_transaction() as s: s["user_id"]=7; s["role"]="owner"
        self.assertEqual(403,self.client.get("/admin/email-analytics").status_code)

    @patch("app.routes.admin.render_template",return_value="dashboard")
    @patch("app.routes.admin.email_analytics.dashboard",return_value={})
    def test_admin_dashboard_route(self,dashboard,_render):
        with self.client.session_transaction() as s: s["user_id"]=1; s["role"]="admin"
        response=self.client.get("/admin/email-analytics?q=owner")
        self.assertEqual(200,response.status_code); dashboard.assert_called_once()

    @patch("app.routes.admin.email_analytics.dashboard")
    def test_dashboard_template_renders_empty_state(self,dashboard):
        summary={"total":0,"today":0,"pending":0,"processing":0,"sent":0,"failed":0,"cancelled":0,"success_rate":0,"avg_send":None,"queue_size":0,"message_ids":0,"unique_customers":0}
        dashboard.return_value={"summary":summary,"type_breakdown":[{"email_type":k,"total":0,"today":0,"last_7":0,"last_30":0,"success_rate":0,"failed_rate":0,"avg_send":None} for k in ("welcome","login_otp","subscription_confirmation","renewal_reminder")],"charts":{"daily":[],"distribution":[],"statuses":[],"hourly":[]},"health":{"success_rate":0,"avg_queue_delay":None,"failures_today":0,"retries_today":0,"cancelled":0,"queue_size":0,"max_retries":0,"oldest_waiting":None},"top_customers":[],"failures":[],"jobs":[],"options":{"users":[],"businesses":[],"plans":[]},"pagination":{"page":1,"pages":1,"total":0,"per_page":25}}
        with self.client.session_transaction() as s: s["user_id"]=1; s["role"]="admin"
        response=self.client.get("/admin/email-analytics")
        self.assertEqual(200,response.status_code); self.assertIn(b"Email Analytics",response.data)
        self.assertIn(b"No email jobs match",response.data)

    @patch("app.routes.admin.email_analytics.export_rows")
    def test_filtered_csv_export_excludes_template_and_otp(self,export):
        export.return_value=[{"customer_name":"Owner","recipient_email":"o@example.com","email_type":"login_otp","status":"sent","template_data":"123456","otp_code":"123456"}]
        with self.client.session_transaction() as s: s["user_id"]=1; s["role"]="admin"
        response=self.client.get("/admin/email-analytics/export.csv?status=sent")
        body=response.get_data(as_text=True)
        self.assertEqual(200,response.status_code); self.assertNotIn("otp_code",body)
        self.assertNotIn("template_data",body); self.assertNotIn("123456",body)
        self.assertIn("attachment",response.headers["Content-Disposition"])


if __name__=="__main__": unittest.main()
