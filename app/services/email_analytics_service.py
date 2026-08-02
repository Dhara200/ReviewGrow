"""Aggregated, paginated, and redacted admin email operations data."""

import json
import math
from datetime import datetime, timedelta

from app.services.database_service import get_connection
from app.utils.plan_display_utils import display_plan_name


EMAIL_TYPES = ("welcome", "login_otp", "subscription_confirmation", "renewal_reminder")
STATUSES = ("pending", "processing", "sent", "failed", "cancelled")
SORTS = {
    "newest": "j.created_at DESC", "oldest": "j.created_at ASC",
    "retries": "j.attempt_count DESC,j.created_at DESC",
    "processing": "processing_seconds DESC,j.created_at DESC",
    "customer": "u.name ASC,j.created_at DESC",
    "type": "j.email_type ASC,j.created_at DESC",
}
SAFE_TEMPLATE_KEYS = {
    "plan_name", "amount_display", "currency", "razorpay_payment_id",
    "razorpay_order_id", "subscription_start_date", "subscription_end_date",
    "days_remaining", "renewal_url", "dashboard_url", "support_email",
    "expected_subscription_end_ist",
}


def normalize_filters(args):
    page = max(args.get("page", type=int) or 1, 1)
    per_page = min(max(args.get("per_page", type=int) or 25, 10), 100)
    email_type = (args.get("email_type") or "").strip()
    status = (args.get("status") or "").strip()
    return {
        "q": (args.get("q") or "").strip()[:120],
        "email_type": email_type if email_type in EMAIL_TYPES else "",
        "status": status if status in STATUSES else "",
        "date_from": _date(args.get("date_from")), "date_to": _date(args.get("date_to")),
        "user_id": args.get("user_id", type=int), "business_id": args.get("business_id", type=int),
        "plan": (args.get("plan") or "").strip()[:50],
        "priority": args.get("priority", type=int), "retry_min": args.get("retry_min", type=int),
        "processing_min": args.get("processing_min", type=int),
        "sort": (args.get("sort") or "newest") if (args.get("sort") or "newest") in SORTS else "newest",
        "page": page, "per_page": per_page,
    }


class EmailAnalyticsService:
    def __init__(self, connection_factory=get_connection): self.connection_factory = connection_factory

    def dashboard(self, filters):
        connection = self.connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            summary = self._summary(cursor)
            types = self._type_breakdown(cursor)
            charts = self._charts(cursor)
            health = self._health(cursor, summary)
            top_customers = self._top_customers(cursor)
            failures = self._top_failures(cursor)
            rows, total = self._jobs(cursor, filters)
            options = self._options(cursor)
        finally:
            cursor.close(); connection.close()
        return {
            "summary": summary, "type_breakdown": types, "charts": charts,
            "health": health, "top_customers": top_customers,
            "failures": failures, "jobs": rows, "options": options,
            "pagination": {"page": filters["page"], "per_page": filters["per_page"],
                           "total": total, "pages": max(1, math.ceil(total / filters["per_page"]))},
        }

    def export_rows(self, filters, limit=10000):
        filters = dict(filters, page=1, per_page=min(limit, 10000))
        connection = self.connection_factory(); cursor = connection.cursor(dictionary=True)
        try: rows, _ = self._jobs(cursor, filters, export=True)
        finally: cursor.close(); connection.close()
        return rows

    def _summary(self, c):
        c.execute("""SELECT COUNT(*) total, SUM(sent_at>=UTC_DATE()) today,
          SUM(status='pending') pending, SUM(status='processing') processing,
          SUM(status='sent') sent, SUM(status='failed') failed,SUM(status='cancelled') cancelled,
          COUNT(DISTINCT CASE WHEN status='sent' THEN user_id END) unique_customers,
          SUM(ses_message_id IS NOT NULL) message_ids,
          AVG(CASE WHEN status='sent' THEN TIMESTAMPDIFF(MICROSECOND,processing_started_at,sent_at)/1000000 END) avg_send,
          MIN(CASE WHEN status='pending' THEN created_at END) oldest_waiting
          FROM email_jobs""")
        r = c.fetchone() or {}; total_terminal = int(r.get("sent") or 0)+int(r.get("failed") or 0)
        r["success_rate"] = round(100*int(r.get("sent") or 0)/total_terminal, 1) if total_terminal else 0
        r["queue_size"] = int(r.get("pending") or 0)+int(r.get("processing") or 0)
        return {k: (round(float(v), 2) if k == "avg_send" and v is not None else int(v or 0) if k not in {"oldest_waiting","success_rate"} else v) for k,v in r.items()}

    def _type_breakdown(self, c):
        c.execute("""SELECT email_type,COUNT(*) total,SUM(created_at>=UTC_DATE()) today,
          SUM(created_at>=UTC_TIMESTAMP()-INTERVAL 7 DAY) last_7,
          SUM(created_at>=UTC_TIMESTAMP()-INTERVAL 30 DAY) last_30,
          SUM(status='sent') sent,SUM(status='failed') failed,
          AVG(CASE WHEN status='sent' THEN TIMESTAMPDIFF(MICROSECOND,processing_started_at,sent_at)/1000000 END) avg_send
          FROM email_jobs GROUP BY email_type""")
        result = {row["email_type"]: row for row in c.fetchall()}
        for kind in EMAIL_TYPES:
            row = result.setdefault(kind, {"email_type": kind, "total": 0, "today": 0, "last_7": 0, "last_30": 0, "sent": 0, "failed": 0, "avg_send": None})
            terminal = int(row.get("sent") or 0)+int(row.get("failed") or 0)
            row["success_rate"] = round(100*int(row.get("sent") or 0)/terminal, 1) if terminal else 0
            row["failed_rate"] = round(100*int(row.get("failed") or 0)/terminal, 1) if terminal else 0
        return [result[k] for k in EMAIL_TYPES]

    def _charts(self, c):
        c.execute("""SELECT DATE(sent_at) label,COUNT(*) value FROM email_jobs
          WHERE status='sent' AND sent_at>=UTC_DATE()-INTERVAL 29 DAY GROUP BY DATE(sent_at) ORDER BY label""")
        daily = [_json_row(r) for r in c.fetchall()]
        c.execute("SELECT email_type label,COUNT(*) value FROM email_jobs GROUP BY email_type")
        distribution = [_json_row(r) for r in c.fetchall()]
        c.execute("SELECT status label,COUNT(*) value FROM email_jobs GROUP BY status")
        statuses = [_json_row(r) for r in c.fetchall()]
        c.execute("SELECT HOUR(created_at) label,COUNT(*) value FROM email_jobs WHERE created_at>=UTC_TIMESTAMP()-INTERVAL 30 DAY GROUP BY HOUR(created_at) ORDER BY label")
        hourly = [_json_row(r) for r in c.fetchall()]
        return {"daily": daily, "distribution": distribution, "statuses": statuses, "hourly": hourly}

    def _health(self, c, summary):
        c.execute("""SELECT SUM(status='failed' AND created_at>=UTC_DATE()) failures_today,
          SUM(attempt_count>1 AND updated_at>=UTC_DATE()) retries_today,
          MAX(attempt_count) max_retries,
          AVG(TIMESTAMPDIFF(MICROSECOND,created_at,processing_started_at)/1000000) avg_queue_delay
          FROM email_jobs""")
        r = c.fetchone() or {}; r.update({"success_rate": summary["success_rate"], "cancelled": summary.get("cancelled",0), "queue_size": summary["queue_size"], "oldest_waiting": summary.get("oldest_waiting")})
        return r

    def _top_customers(self, c):
        c.execute("""SELECT u.name,u.email,COUNT(*) total,SUM(j.email_type='welcome') welcome,
          SUM(j.email_type='login_otp') otp,SUM(j.email_type='subscription_confirmation') payments,
          SUM(j.email_type='renewal_reminder') renewals,SUM(j.status='failed') failures,MAX(j.created_at) last_email
          FROM email_jobs j JOIN users u ON u.id=j.user_id GROUP BY u.id,u.name,u.email ORDER BY total DESC LIMIT 10""")
        return c.fetchall()

    def _top_failures(self, c):
        c.execute("""SELECT COALESCE(NULLIF(last_error,''),'Unclassified delivery failure') reason,
          COUNT(*) count,MAX(updated_at) last_occurrence FROM email_jobs WHERE status='failed'
          GROUP BY COALESCE(NULLIF(last_error,''),'Unclassified delivery failure') ORDER BY count DESC LIMIT 8""")
        rows=c.fetchall(); total=sum(int(r["count"]) for r in rows)
        for r in rows: r["percentage"] = round(100*int(r["count"])/total,1) if total else 0; r["reason"] = _safe_error(r["reason"])
        return rows

    def _jobs(self, c, f, export=False):
        where, params = _where(f)
        c.execute(f"SELECT COUNT(*) total FROM email_jobs j LEFT JOIN users u ON u.id=j.user_id WHERE {where}", tuple(params)); total=int((c.fetchone() or {}).get("total") or 0)
        limit = min(int(f["per_page"]), 10000); offset=(int(f["page"])-1)*limit
        c.execute(f"""SELECT j.id,u.name customer_name,j.recipient_email,
          (SELECT b.business_name FROM businesses b WHERE b.user_id=j.user_id ORDER BY b.id LIMIT 1) business_name,
          j.email_type,j.template_name,j.priority,j.status,j.created_at,j.processing_started_at,j.sent_at,
          TIMESTAMPDIFF(MICROSECOND,j.processing_started_at,j.sent_at)/1000000 processing_seconds,
          TIMESTAMPDIFF(MICROSECOND,j.created_at,COALESCE(j.processing_started_at,UTC_TIMESTAMP(6)))/1000000 queue_delay_seconds,
          j.attempt_count,j.max_attempts,j.ses_message_id,j.last_error,j.deduplication_key,j.template_data,
          (SELECT s.plan_name FROM subscriptions s WHERE s.user_id=j.user_id ORDER BY s.created_at DESC,s.id DESC LIMIT 1) subscription_plan
          FROM email_jobs j LEFT JOIN users u ON u.id=j.user_id WHERE {where}
          ORDER BY {SORTS[f['sort']]} LIMIT %s OFFSET %s""", tuple(params+[limit,offset]))
        rows=c.fetchall()
        for row in rows:
            data=_json_data(row.pop("template_data", None)); row["safe_template_variables"]={k:(display_plan_name(data[k]) if k=="plan_name" else data[k]) for k in SAFE_TEMPLATE_KEYS if k in data}; row["payment_id"]=data.get("razorpay_payment_id") if row["email_type"]=="subscription_confirmation" else None; row["last_error"]=_safe_error(row.get("last_error")); row["retry_count"]=max(int(row.get("attempt_count") or 0)-1,0); row["subscription_plan"]=display_plan_name(row.get("subscription_plan"))
        return rows,total

    def _options(self,c):
        c.execute("SELECT id,name,email FROM users ORDER BY name LIMIT 500"); users=c.fetchall()
        c.execute("SELECT id,business_name FROM businesses ORDER BY business_name LIMIT 500"); businesses=c.fetchall()
        c.execute("SELECT DISTINCT plan_name FROM subscriptions ORDER BY plan_name"); plans=[{"value":r["plan_name"],"label":display_plan_name(r["plan_name"])} for r in c.fetchall()]
        return {"users":users,"businesses":businesses,"plans":plans}


def _where(f):
    clauses=["1=1"]; params=[]
    if f["q"]: clauses.append("(u.name LIKE %s OR j.recipient_email LIKE %s OR j.ses_message_id LIKE %s OR EXISTS(SELECT 1 FROM businesses bx WHERE bx.user_id=j.user_id AND bx.business_name LIKE %s) OR JSON_UNQUOTE(JSON_EXTRACT(j.template_data,'$.razorpay_payment_id')) LIKE %s)"); params += [f"%{f['q']}%"]*5
    for key,column in (("email_type","j.email_type"),("status","j.status"),("user_id","j.user_id"),("priority","j.priority")):
        if f.get(key) not in (None,""): clauses.append(f"{column}=%s"); params.append(f[key])
    if f["business_id"]: clauses.append("EXISTS(SELECT 1 FROM businesses bf WHERE bf.id=%s AND bf.user_id=j.user_id)"); params.append(f["business_id"])
    if f["plan"]: clauses.append("EXISTS(SELECT 1 FROM subscriptions sf WHERE sf.user_id=j.user_id AND sf.plan_name=%s)"); params.append(f["plan"])
    if f["date_from"]: clauses.append("j.created_at >= %s"); params.append(f["date_from"])
    if f["date_to"]: clauses.append("j.created_at < DATE_ADD(%s,INTERVAL 1 DAY)"); params.append(f["date_to"])
    if f["retry_min"] is not None: clauses.append("j.attempt_count >= %s"); params.append(max(f["retry_min"]+1,1))
    if f["processing_min"] is not None: clauses.append("TIMESTAMPDIFF(SECOND,j.processing_started_at,j.sent_at) >= %s"); params.append(max(f["processing_min"],0))
    return " AND ".join(clauses),params


def _date(value):
    try: return datetime.strptime(value,"%Y-%m-%d").date() if value else None
    except ValueError: return None
def _json_data(value):
    if isinstance(value,dict): return value
    try: return json.loads(value or "{}")
    except (TypeError,ValueError): return {}
def _json_row(row):
    return {"label": row["label"].isoformat() if hasattr(row["label"],"isoformat") else row["label"], "value": int(row["value"] or 0)}
def _safe_error(value):
    text=" ".join(str(value or "").split())[:180]
    for token in ("otp","password","secret","signature","token"):
        if token in text.lower(): return "Sensitive delivery error redacted"
    return text
