"""Read-only, failure-isolated infrastructure monitoring.

The provider boundary intentionally keeps Flask unaware of EC2/Docker details so
an ECS/CloudWatch provider can replace this implementation later.
"""
from __future__ import annotations

import copy
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.database_service import get_connection

try:
    import psutil
except ImportError:  # pragma: no cover - production dependency, graceful fallback
    psutil = None

logger = logging.getLogger(__name__)
UNAVAILABLE = "Unavailable"
IST = ZoneInfo("Asia/Kolkata")
_STARTED_AT = time.time()
_CACHE_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "value": None}
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe(callable_, fallback):
    try:
        return callable_()
    except Exception as error:
        logger.warning("Infrastructure collector unavailable: collector=%s error_type=%s",
                       getattr(callable_, "__name__", "unknown"), type(error).__name__)
        return copy.deepcopy(fallback)


def _bytes(value):
    return int(value) if value is not None else None


class InfrastructureProvider(ABC):
    @abstractmethod
    def collect(self):
        raise NotImplementedError


class EC2DockerInfrastructureProvider(InfrastructureProvider):
    """Local host/process/database provider. It never mutates runtime state."""

    def collect(self):
        application = _safe(self.collect_application_metrics, self._application_unavailable())
        host = _safe(self.collect_host_metrics, self._host_unavailable())
        docker = _safe(self.collect_docker_metrics, self._docker_unavailable())
        gunicorn = _safe(self.collect_gunicorn_metrics, self._gunicorn_unavailable())
        workers = _safe(self.collect_worker_metrics, self._worker_unavailable())
        database = _safe(self.collect_database_metrics, self._database_unavailable())
        jobs = _safe(self.collect_job_metrics, self._jobs_unavailable())
        result = {
            "application": application, "host": host, "docker": docker,
            "gunicorn": gunicorn, "background_workers": workers,
            "database": database, "jobs": jobs,
        }
        result["alerts"] = self.build_alerts(result)
        result["overall_status"] = self._overall_status(result["alerts"])
        result["refreshed_at_ist"] = datetime.now(IST).isoformat(timespec="seconds")
        return result

    @staticmethod
    def _application_unavailable():
        return {"status": "unavailable", "environment": UNAVAILABLE, "version": UNAVAILABLE,
                "uptime_seconds": None, "timestamp_ist": UNAVAILABLE,
                "health_check": {"status": "unavailable", "database": "unavailable"}}

    @staticmethod
    def _host_unavailable():
        return {"cpu_percent": None, "load_average": {"1m": None, "5m": None, "15m": None},
                "memory": {}, "swap": {}, "disk": {}, "uptime_seconds": None,
                "architecture": UNAVAILABLE, "hostname": "Omitted"}

    @staticmethod
    def _docker_unavailable():
        return {"available": False, "reason": "Docker inspection is not available to this process.",
                "containers": {name: None for name in ("app", "worker", "mysql")}}

    @staticmethod
    def _gunicorn_unavailable():
        return {"configured_workers": None, "running_workers": None, "worker_class": UNAVAILABLE,
                "configured_threads": None, "master_status": "unavailable"}

    @staticmethod
    def _worker_unavailable():
        return {"configured_workers": None, "running_workers": None,
                "health": "unavailable", "oldest_uptime_seconds": None, "last_heartbeat": None}

    @staticmethod
    def _database_unavailable():
        return {"reachable": False, "active_connections": None, "max_connections": None,
                "running_threads": None, "slow_queries": None, "size_bytes": None,
                "health_check_duration_ms": None}

    @staticmethod
    def _jobs_unavailable():
        return {"available": False, "running": None, "pending": None, "failed": None,
                "completed_today": None, "oldest_pending_age_seconds": None, "by_queue": {}}

    def collect_application_metrics(self):
        version = next((os.getenv(key, "").strip() for key in ("APP_VERSION", "IMAGE_TAG", "GIT_SHA")
                        if os.getenv(key, "").strip()), UNAVAILABLE)
        if version != UNAVAILABLE and not _SAFE_VERSION.fullmatch(version):
            version = UNAVAILABLE
        db_health = self._database_health_only()
        return {"status": "healthy" if db_health[0] else "degraded",
                "environment": os.getenv("APP_ENV", "development") if os.getenv("APP_ENV", "development") in {"development", "testing", "staging", "production"} else UNAVAILABLE,
                "version": version, "uptime_seconds": round(time.time() - _STARTED_AT),
                "timestamp_ist": datetime.now(IST).isoformat(timespec="seconds"),
                "health_check": {"status": "healthy" if db_health[0] else "unhealthy",
                                 "database": "reachable" if db_health[0] else "unreachable"}}

    def collect_host_metrics(self):
        if psutil is None:
            raise RuntimeError("psutil unavailable")
        memory, swap, disk = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
        try:
            loads = psutil.getloadavg()
        except (AttributeError, OSError):
            loads = (None, None, None)
        return {"cpu_percent": psutil.cpu_percent(interval=0.1),
                "load_average": {"1m": loads[0], "5m": loads[1], "15m": loads[2]},
                "memory": {"total": _bytes(memory.total), "used": _bytes(memory.used),
                           "available": _bytes(memory.available), "percent": float(memory.percent)},
                "swap": {"total": _bytes(swap.total), "used": _bytes(swap.used),
                         "free": _bytes(swap.free), "percent": float(swap.percent),
                         "sin": _bytes(getattr(swap, "sin", 0)), "sout": _bytes(getattr(swap, "sout", 0))},
                "disk": {"total": _bytes(disk.total), "used": _bytes(disk.used),
                         "available": _bytes(disk.free), "percent": float(disk.percent)},
                "uptime_seconds": round(time.time() - psutil.boot_time()),
                "architecture": platform.machine() or UNAVAILABLE, "hostname": "Omitted"}

    def _processes(self):
        if psutil is None:
            return []
        found = []
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
            try:
                info = proc.info
                found.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

    def collect_gunicorn_metrics(self):
        processes = self._processes()
        gunicorn = [p for p in processes if "gunicorn" in ((p.get("name") or "") + " " + " ".join(p.get("cmdline") or [])).lower()]
        masters = [p for p in gunicorn if p.get("ppid") not in {g.get("pid") for g in gunicorn}]
        workers = [p for p in gunicorn if p.get("ppid") in {m.get("pid") for m in masters}]
        args = (masters[0].get("cmdline") or []) if masters else []
        def option(*names):
            for i, arg in enumerate(args[:-1]):
                if arg in names:
                    return args[i + 1]
            return None
        configured = option("--workers", "-w")
        threads = option("--threads")
        worker_class = option("--worker-class", "-k") or ("sync" if masters else UNAVAILABLE)
        return {"configured_workers": int(configured) if configured and configured.isdigit() else None,
                "running_workers": len(workers) if masters else None, "worker_class": worker_class,
                "configured_threads": int(threads) if threads and threads.isdigit() else (1 if masters else None),
                "master_status": "running" if masters else "unavailable"}

    def collect_worker_metrics(self):
        workers = [p for p in self._processes() if any(Path(arg).name == "worker.py" for arg in (p.get("cmdline") or []))]
        configured_raw = os.getenv("BACKGROUND_WORKER_COUNT", "1")
        configured = int(configured_raw) if configured_raw.isdigit() else None
        uptimes = [time.time() - p["create_time"] for p in workers if p.get("create_time")]
        return {"configured_workers": configured, "running_workers": len(workers),
                "health": "healthy" if workers else "unavailable",
                "oldest_uptime_seconds": round(max(uptimes)) if uptimes else None,
                "last_heartbeat": None}

    def collect_docker_metrics(self):
        # Never adds socket access. A fixed, read-only CLI call is used only when
        # the runtime already has a Docker socket and client.
        socket_path = Path("/var/run/docker.sock")
        docker = shutil.which("docker")
        if not socket_path.exists() or not docker:
            return self._docker_unavailable()
        command = [docker, "inspect", "reputation_app", "reputation_worker", "reputation_mysql",
                   "--format", "{{json .}}"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=2,
                                   check=False, shell=False)
        if completed.returncode != 0:
            return self._docker_unavailable()
        import json
        containers = {name: None for name in ("app", "worker", "mysql")}
        for line in completed.stdout.splitlines()[:3]:
            raw = json.loads(line)
            name = str(raw.get("Name", "")).lstrip("/")
            key = next((k for k in containers if k in name), None)
            if key:
                state = raw.get("State") or {}
                containers[key] = {"status": state.get("Status", UNAVAILABLE),
                    "health": (state.get("Health") or {}).get("Status", UNAVAILABLE),
                    "restart_count": int(raw.get("RestartCount") or 0),
                    "started_at": state.get("StartedAt") or UNAVAILABLE}
        return {"available": True, "reason": None, "containers": containers}

    def _database_health_only(self):
        started = time.perf_counter()
        conn = cursor = None
        try:
            conn = get_connection(); cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT 1 AS ok")
            return bool((cursor.fetchone() or {}).get("ok")), round((time.perf_counter() - started) * 1000, 2)
        except Exception:
            return False, round((time.perf_counter() - started) * 1000, 2)
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def collect_database_metrics(self):
        started = time.perf_counter(); conn = cursor = None
        result = self._database_unavailable()
        try:
            conn = get_connection(); cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT 1 AS ok"); cursor.fetchone()
            result["reachable"] = True
            try:
                cursor.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running','Slow_queries')")
                status = {row["Variable_name"]: int(row["Value"]) for row in cursor.fetchall()}
                result.update(active_connections=status.get("Threads_connected"), running_threads=status.get("Threads_running"), slow_queries=status.get("Slow_queries"))
            except Exception:
                pass
            try:
                cursor.execute("SHOW VARIABLES LIKE 'max_connections'")
                row = cursor.fetchone() or {}; result["max_connections"] = int(row.get("Value")) if row.get("Value") else None
            except Exception:
                pass
            try:
                cursor.execute("SELECT SUM(data_length + index_length) AS size_bytes FROM information_schema.tables WHERE table_schema=DATABASE()")
                result["size_bytes"] = _bytes((cursor.fetchone() or {}).get("size_bytes"))
            except Exception:
                pass
            return result
        finally:
            result["health_check_duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if cursor: cursor.close()
            if conn: conn.close()

    def collect_job_metrics(self):
        conn = cursor = None
        queues = {
            "google_review_sync": ("google_review_sync_jobs", "completed"),
            "analysis": ("analysis_jobs", "completed"),
            "email": ("email_jobs", "sent"),
        }
        by_queue = {}
        try:
            conn = get_connection(); cursor = conn.cursor(dictionary=True)
            for label, (table, completed_state) in queues.items():
                # Table/state identifiers are compile-time allowlisted; no user input.
                cursor.execute(f"""SELECT
                    SUM(status='processing') AS running,
                    SUM(status='pending') AS pending,
                    SUM(status='failed') AS failed,
                    SUM(status=%s AND completed_at >= UTC_DATE()) AS completed_today,
                    TIMESTAMPDIFF(SECOND, MIN(CASE WHEN status='pending' THEN created_at END), UTC_TIMESTAMP()) AS oldest_pending_age_seconds
                    FROM {table}""".replace("completed_at", "sent_at" if table == "email_jobs" else "completed_at"), (completed_state,))
                row = cursor.fetchone() or {}
                by_queue[label] = {key: int(row.get(key) or 0) if key != "oldest_pending_age_seconds" or row.get(key) is not None else None
                                         for key in ("running", "pending", "failed", "completed_today", "oldest_pending_age_seconds")}
            totals = {key: sum(q.get(key) or 0 for q in by_queue.values()) for key in ("running", "pending", "failed", "completed_today")}
            ages = [q["oldest_pending_age_seconds"] for q in by_queue.values() if q.get("oldest_pending_age_seconds") is not None]
            # job_type is indexed by the existing lease/job-type migration. This
            # extra aggregate exposes the existing competitor queue without rows.
            try:
                cursor.execute("""SELECT SUM(status='processing') AS running,
                    SUM(status='pending') AS pending, SUM(status='failed') AS failed,
                    SUM(status='completed' AND completed_at >= UTC_DATE()) AS completed_today,
                    TIMESTAMPDIFF(SECOND, MIN(CASE WHEN status='pending' THEN created_at END), UTC_TIMESTAMP()) AS oldest_pending_age_seconds
                    FROM analysis_jobs WHERE job_type='competitor_refresh'""")
                row = cursor.fetchone() or {}
                by_queue["competitor_analytics"] = {key: int(row.get(key) or 0) if key != "oldest_pending_age_seconds" or row.get(key) is not None else None
                                                     for key in ("running", "pending", "failed", "completed_today", "oldest_pending_age_seconds")}
            except Exception:
                # Older compatible deployments may not yet expose job_type.
                pass
            return {"available": True, **totals, "oldest_pending_age_seconds": max(ages) if ages else None, "by_queue": by_queue}
        finally:
            if cursor: cursor.close()
            if conn: conn.close()

    def build_alerts(self, data):
        alerts = []
        def add(condition, code, message, severity="warning"):
            if condition: alerts.append({"code": code, "severity": severity, "message": message})
        host = data["host"]
        add((host.get("cpu_percent") or 0) > 75, "cpu_high", "CPU usage is above 75%.")
        add((host.get("memory") or {}).get("percent", 0) > 80, "memory_high", "Memory usage is above 80%.")
        add((host.get("disk") or {}).get("percent", 0) > 80, "disk_high", "Root filesystem usage is above 80%.")
        swap = host.get("swap") or {}
        add((swap.get("percent") or 0) > 20 or (swap.get("sin") or 0) > 0 or (swap.get("sout") or 0) > 0,
            "swap_used", "Swap is significantly used or has paging activity.")
        for key, label in (("gunicorn", "Gunicorn"), ("background_workers", "Background worker")):
            item = data[key]; configured, running = item.get("configured_workers"), item.get("running_workers")
            add(configured is not None and running is not None and configured != running,
                f"{key}_mismatch", f"{label} configured and running counts do not match.")
        for name, container in (data["docker"].get("containers") or {}).items():
            add(container is not None and (container.get("status") != "running" or container.get("health") == "unhealthy"),
                f"container_{name}", f"{name.title()} container is not healthy.", "critical")
        jobs = data["jobs"]
        add((jobs.get("oldest_pending_age_seconds") or 0) > 900, "queue_stale", "The oldest pending job is more than 15 minutes old.")
        add((jobs.get("pending") or 0) > 100, "queue_depth", "Pending job depth is above 100.")
        add(not data["database"].get("reachable"), "database_unavailable", "Database health check failed.", "critical")
        return alerts

    @staticmethod
    def _overall_status(alerts):
        if any(a["severity"] == "critical" for a in alerts): return "critical"
        if alerts: return "warning"
        return "healthy"


def get_infrastructure_status(provider=None, use_cache=True):
    """Return a secret-free status snapshot cached briefly for polling clients."""
    provider = provider or EC2DockerInfrastructureProvider()
    now = time.monotonic()
    with _CACHE_LOCK:
        if use_cache and _CACHE["value"] is not None and now - _CACHE["at"] < 7:
            return copy.deepcopy(_CACHE["value"])
    value = provider.collect()
    with _CACHE_LOCK:
        _CACHE.update(at=now, value=copy.deepcopy(value))
    return value
