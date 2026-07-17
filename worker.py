import logging
import re
import time
from datetime import datetime

from app.config import Config
from app.services.analysis_job_service import (
    claim_next_job,
    process_analysis_job,
    reset_stale_processing_jobs,
)
from app.services.database_service import ensure_mvp_schema
from app.services.google_review_sync_execution_service import run_google_review_sync
from app.services.google_review_sync_job_service import GoogleReviewSyncJobService


google_review_sync_jobs = GoogleReviewSyncJobService()
logger = logging.getLogger(__name__)


def run_worker_iteration():
    """Process at most one job from each queue so neither queue is starved."""
    processed_job = False
    pending_sync_job = google_review_sync_jobs.get_oldest_pending_job()

    if pending_sync_job and google_review_sync_jobs.claim_job(pending_sync_job["id"]):
        _process_google_review_sync_job(pending_sync_job)
        processed_job = True

    analysis_job = claim_next_job()
    if analysis_job:
        process_analysis_job(
            analysis_job["id"],
            batch_size=Config.AI_BATCH_SIZE,
        )
        processed_job = True

    return processed_job


def run_worker_forever():
    while True:
        reset_stale_processing_jobs()
        if not run_worker_iteration():
            time.sleep(Config.AI_WORKER_POLL_SECONDS)


def _process_google_review_sync_job(job):
    try:
        result = run_google_review_sync(job["user_id"], job["business_id"])
        google_review_sync_jobs.update_job(
            job["id"],
            status="completed",
            fetched_count=result["fetched_count"],
            inserted_count=result["inserted_count"],
            updated_count=result["updated_count"],
            completed_at=datetime.utcnow(),
            error_message=None,
        )
    except Exception as error:
        safe_message = _safe_error_message(error)
        sanitized_exception = RuntimeError(safe_message)
        logger.error(
            "Google review sync job failed: job_id=%s business_id=%s",
            job.get("id"),
            job.get("business_id"),
            exc_info=(type(sanitized_exception), sanitized_exception, error.__traceback__),
        )
        google_review_sync_jobs.update_job(
            job["id"],
            status="failed",
            completed_at=datetime.utcnow(),
            error_message=safe_message,
        )


def _safe_error_message(error):
    message = " ".join(str(error).split()) or error.__class__.__name__
    patterns = (
        r"(?i)(bearer\s+)[^\s,;]+",
        r"(?i)((?:access|refresh)[_-]?token\s*[=:]\s*)[^\s,;]+",
        r"(?i)(client[_-]?secret\s*[=:]\s*)[^\s,;]+",
        r"(?i)(authorization\s*[=:]\s*)[^\s,;]+",
    )
    for pattern in patterns:
        message = re.sub(pattern, r"\1[REDACTED]", message)
    return message[:500]


if __name__ == "__main__":
    try:
        ensure_mvp_schema()
    except Exception as error:
        print(f"Schema check skipped: {error}", flush=True)

    print("Background worker started.", flush=True)
    run_worker_forever()
