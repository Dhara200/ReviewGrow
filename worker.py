import logging
import random
import re
import signal
import time
from datetime import datetime

import requests

from app.config import Config
from app.services.analysis_job_service import (
    claim_next_job,
    process_analysis_job,
    reset_stale_processing_jobs,
)
from app.services.database_service import ensure_mvp_schema
from app.services.google_review_sync_execution_service import run_google_review_sync
from app.services.google_review_sync_job_service import GoogleReviewSyncJobService
from app.services.google_business_service import GoogleQuotaError, GoogleTransientError


google_review_sync_jobs = GoogleReviewSyncJobService()
logger = logging.getLogger(__name__)
shutdown_requested = False


def run_worker_iteration():
    """Process at most one job from each queue so neither queue is starved."""
    processed_job = False
    pending_sync_job = google_review_sync_jobs.get_oldest_pending_job()

    if pending_sync_job and google_review_sync_jobs.claim_job(pending_sync_job["id"]):
        logger.info(
            "Google review sync job claimed: job_id=%s business_id=%s",
            pending_sync_job.get("id"),
            pending_sync_job.get("business_id"),
        )
        _process_google_review_sync_job(pending_sync_job)
        processed_job = True

    if not shutdown_requested:
        analysis_job = claim_next_job()
        if analysis_job:
            process_analysis_job(
                analysis_job["id"],
                batch_size=Config.AI_BATCH_SIZE,
            )
            processed_job = True

    return processed_job


def run_worker_forever():
    global shutdown_requested
    recovered_count = google_review_sync_jobs.recover_stale_processing_jobs(
        Config.GOOGLE_REVIEW_SYNC_STALE_TIMEOUT_MINUTES
    )
    logger.info("Recovered stale Google review sync jobs: count=%s", recovered_count)

    while not shutdown_requested:
        reset_stale_processing_jobs()
        if not run_worker_iteration():
            time.sleep(Config.AI_WORKER_POLL_SECONDS)

    logger.info("Background worker stopped cleanly.")


def _process_google_review_sync_job(job):
    started_at = time.monotonic()
    logger.info(
        "Google review sync job started: job_id=%s business_id=%s",
        job.get("id"),
        job.get("business_id"),
    )
    try:
        result = _run_google_review_sync_with_retries(job)
        google_review_sync_jobs.update_job(
            job["id"],
            status="completed",
            fetched_count=result["fetched_count"],
            inserted_count=result["inserted_count"],
            updated_count=result["updated_count"],
            completed_at=datetime.utcnow(),
            error_message=None,
        )
        logger.info(
            "Google review sync job completed: job_id=%s fetched=%s inserted=%s "
            "updated=%s elapsed_seconds=%.3f",
            job.get("id"),
            result["fetched_count"],
            result["inserted_count"],
            result["updated_count"],
            time.monotonic() - started_at,
        )
    except Exception as error:
        safe_message = _safe_error_message(error)
        sanitized_exception = RuntimeError(safe_message)
        logger.error(
            "Google review sync job failed: job_id=%s business_id=%s elapsed_seconds=%.3f",
            job.get("id"),
            job.get("business_id"),
            time.monotonic() - started_at,
            exc_info=(type(sanitized_exception), sanitized_exception, error.__traceback__),
        )
        google_review_sync_jobs.update_job(
            job["id"],
            status="failed",
            completed_at=datetime.utcnow(),
            error_message=safe_message,
        )


def _run_google_review_sync_with_retries(job, sleep=time.sleep, jitter=random.uniform):
    max_retries = Config.GOOGLE_REVIEW_SYNC_MAX_RETRIES
    for retry_count in range(max_retries + 1):
        try:
            return run_google_review_sync(job["user_id"], job["business_id"])
        except Exception as error:
            if not _is_retryable_sync_error(error) or retry_count == max_retries:
                raise

            retry_number = retry_count + 1
            delay = _retry_backoff_seconds(retry_number, jitter=jitter)
            logger.warning(
                "Retrying Google review sync job: job_id=%s retry=%s/%s "
                "backoff_seconds=%.3f error_type=%s",
                job.get("id"),
                retry_number,
                max_retries,
                delay,
                error.__class__.__name__,
            )
            sleep(delay)


def _is_retryable_sync_error(error):
    return isinstance(
        error,
        (
            GoogleQuotaError,
            GoogleTransientError,
            requests.Timeout,
            requests.ConnectionError,
        ),
    )


def _retry_backoff_seconds(retry_number, jitter=random.uniform):
    if retry_number < 1:
        raise ValueError("Retry number must be at least one.")
    base_delay = Config.GOOGLE_REVIEW_SYNC_BACKOFF_BASE_SECONDS * (2 ** (retry_number - 1))
    return base_delay + jitter(0, Config.GOOGLE_REVIEW_SYNC_BACKOFF_JITTER_SECONDS)


def _request_shutdown(signum, _frame):
    global shutdown_requested
    shutdown_requested = True
    logger.info("Worker shutdown requested: signal=%s; finishing current job.", signum)


def _install_signal_handlers():
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)


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
    _install_signal_handlers()
    try:
        ensure_mvp_schema()
    except Exception as error:
        print(f"Schema check skipped: {error}", flush=True)

    print("Background worker started.", flush=True)
    run_worker_forever()
