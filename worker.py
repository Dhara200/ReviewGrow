import logging
import os
import random
import re
import signal
import socket
import threading
import time
import uuid
import requests

from app.config import Config
from app.services.analysis_job_service import (
    claim_next_job,
    process_analysis_job,
    reset_stale_processing_jobs,
)
from app.services.database_service import ensure_mvp_schema
from app.services.google_review_sync_execution_service import run_google_review_sync
from app.services.google_review_post_sync_service import perform_google_review_post_sync
from app.services.google_review_sync_job_service import GoogleReviewSyncJobService
from app.services.google_business_service import (
    GoogleBusinessError,
    GoogleQuotaError,
    GoogleTransientError,
)


google_review_sync_jobs = GoogleReviewSyncJobService()
logger = logging.getLogger(__name__)
shutdown_requested = False
shutdown_event = threading.Event()
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"[:255]


class WorkerInfrastructureError(Exception):
    """Signals an iteration failure after local cleanup and safe logging."""


def run_worker_iteration():
    """Process at most one job from each queue so neither queue is starved."""
    processed_job = False
    if shutdown_requested:
        return False
    pending_sync_job = google_review_sync_jobs.get_oldest_pending_job()

    if (
        pending_sync_job
        and not shutdown_requested
        and google_review_sync_jobs.claim_job(
            pending_sync_job["id"],
            WORKER_ID,
            Config.GOOGLE_REVIEW_SYNC_LEASE_SECONDS,
        )
    ):
        logger.info(
            "Google review sync job claimed: job_id=%s business_id=%s worker_id=%s",
            pending_sync_job.get("id"),
            pending_sync_job.get("business_id"),
            WORKER_ID,
        )
        if _process_google_review_sync_job(pending_sync_job, WORKER_ID) is False:
            raise WorkerInfrastructureError(
                "Google review sync terminal state could not be persisted."
            )
        processed_job = True

    if not shutdown_requested:
        analysis_job = claim_next_job()
        if analysis_job:
            analysis_result = process_analysis_job(
                analysis_job["id"],
                batch_size=Config.AI_BATCH_SIZE,
            )
            if analysis_result is False:
                raise WorkerInfrastructureError(
                    "AI analysis job state or infrastructure operation failed."
                )
            processed_job = True

    return processed_job


def run_worker_forever():
    if not _recover_stale_google_jobs():
        logger.info("Worker shutdown during Google stale-job recovery.")
        return

    while not shutdown_requested:
        try:
            reset_stale_processing_jobs()
            if not run_worker_iteration():
                _wait_for_shutdown(Config.AI_WORKER_POLL_SECONDS)
        except Exception as error:
            _log_sanitized_exception(
                "Worker loop infrastructure failure; component=polling_iteration",
                error,
            )
            if _wait_for_shutdown(Config.WORKER_ERROR_BACKOFF_SECONDS):
                logger.info("Worker shutdown during loop error backoff.")
                break
            logger.info(
                "Worker continuing after error backoff: component=polling_iteration "
                "backoff_seconds=%s",
                Config.WORKER_ERROR_BACKOFF_SECONDS,
            )

    logger.info("Background worker stopped cleanly.")


def _process_google_review_sync_job(job, worker_id=WORKER_ID):
    started_at = time.monotonic()
    heartbeat = GoogleReviewSyncHeartbeat(
        google_review_sync_jobs,
        job,
        worker_id,
        Config.GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS,
        Config.GOOGLE_REVIEW_SYNC_LEASE_SECONDS,
    )
    logger.info(
        "Google review sync job started: job_id=%s business_id=%s worker_id=%s",
        job.get("id"),
        job.get("business_id"),
        worker_id,
    )
    execution_error = None
    result = None
    heartbeat.start()
    try:
        result = _run_google_review_sync_with_retries(job)
        perform_google_review_post_sync(
            job["user_id"],
            job["business_id"],
            result,
            result.get("google_location_id"),
        )
    except Exception as error:
        execution_error = error
    finally:
        heartbeat.stop()

    if heartbeat.ownership_lost:
        logger.warning(
            "Google review sync ownership lost; terminal update skipped: "
            "job_id=%s business_id=%s worker_id=%s",
            job.get("id"),
            job.get("business_id"),
            worker_id,
        )
        return True

    if execution_error is not None:
        safe_message = _safe_error_message(execution_error)
        _log_sanitized_exception(
            "Google review sync job failed: job_id=%s business_id=%s "
            "worker_id=%s elapsed_seconds=%.3f",
            execution_error,
            job.get("id"),
            job.get("business_id"),
            worker_id,
            time.monotonic() - started_at,
        )
        try:
            finalized = google_review_sync_jobs.fail_job(
                job["id"],
                worker_id,
                safe_message,
            )
        except Exception as persistence_error:
            _log_sanitized_exception(
                "Unable to persist failed Google review sync status: "
                "job_id=%s business_id=%s component=google_terminal_state",
                persistence_error,
                job.get("id"),
                job.get("business_id"),
            )
            return False
        if not finalized:
            logger.warning(
                "Google review sync failure not persisted because ownership was lost: "
                "job_id=%s business_id=%s worker_id=%s",
                job.get("id"),
                job.get("business_id"),
                worker_id,
            )
        return True

    try:
        finalized = google_review_sync_jobs.complete_job(
            job["id"],
            worker_id,
            result,
        )
    except Exception as persistence_error:
        _log_sanitized_exception(
            "Unable to persist completed Google review sync status: "
            "job_id=%s business_id=%s component=google_terminal_state",
            persistence_error,
            job.get("id"),
            job.get("business_id"),
        )
        return False

    if not finalized:
        logger.warning(
            "Google review sync completion not persisted because ownership was lost: "
            "job_id=%s business_id=%s worker_id=%s",
            job.get("id"),
            job.get("business_id"),
            worker_id,
        )
        return True

    logger.info(
        "Google review sync job completed: job_id=%s fetched=%s inserted=%s "
        "updated=%s elapsed_seconds=%.3f",
        job.get("id"),
        result["fetched_count"],
        result["inserted_count"],
        result["updated_count"],
        time.monotonic() - started_at,
    )
    return True


class GoogleReviewSyncHeartbeat:
    def __init__(self, job_service, job, worker_id, interval_seconds, lease_seconds):
        self._job_service = job_service
        self._job = job
        self._worker_id = worker_id
        self._interval_seconds = interval_seconds
        self._lease_seconds = lease_seconds
        self._stop_event = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"google-review-sync-heartbeat-{job.get('id')}",
            daemon=True,
        )

    @property
    def ownership_lost(self):
        return self._ownership_lost.is_set()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join()

    def _run(self):
        while not self._stop_event.wait(self._interval_seconds):
            try:
                renewed = self._job_service.heartbeat_job(
                    self._job["id"],
                    self._worker_id,
                    self._lease_seconds,
                )
            except Exception as error:
                _log_sanitized_exception(
                    "Google review sync heartbeat failed temporarily: "
                    "job_id=%s business_id=%s worker_id=%s",
                    error,
                    self._job.get("id"),
                    self._job.get("business_id"),
                    self._worker_id,
                )
                continue

            if not renewed:
                self._ownership_lost.set()
                logger.warning(
                    "Google review sync heartbeat lost ownership: "
                    "job_id=%s business_id=%s worker_id=%s",
                    self._job.get("id"),
                    self._job.get("business_id"),
                    self._worker_id,
                )
                return


def _recover_stale_google_jobs():
    while not shutdown_requested:
        try:
            recovered_count = google_review_sync_jobs.recover_expired_processing_jobs(
                Config.GOOGLE_REVIEW_SYNC_STALE_TIMEOUT_MINUTES
            )
            logger.info(
                "Recovered expired Google review sync jobs: count=%s worker_id=%s",
                recovered_count,
                WORKER_ID,
            )
            return True
        except Exception as error:
            _log_sanitized_exception(
                "Google stale-job recovery failed: component=startup_recovery",
                error,
            )
            logger.warning(
                "Retrying Google stale-job recovery after backoff: "
                "backoff_seconds=%s",
                Config.WORKER_ERROR_BACKOFF_SECONDS,
            )
            if _wait_for_shutdown(Config.WORKER_ERROR_BACKOFF_SECONDS):
                return False
    return False


def _wait_for_shutdown(seconds):
    try:
        delay = max(0.0, float(seconds))
    except (TypeError, ValueError):
        delay = 0.0
    return shutdown_event.wait(delay)


def _log_sanitized_exception(message, error, *args):
    safe_exception = RuntimeError(_safe_error_message(error))
    logger.error(
        message,
        *args,
        exc_info=(type(safe_exception), safe_exception, error.__traceback__),
    )


def _run_google_review_sync_with_retries(job, sleep=None, jitter=random.uniform):
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
            if sleep is not None:
                sleep(delay)
            elif _wait_for_shutdown(delay):
                raise GoogleBusinessError(
                    "Worker shutdown requested during Google retry backoff."
                )


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
    shutdown_event.set()
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
        r"(?i)(password\s*[=:]\s*)[^\s,;]+",
        r"(?i)(authorization\s*[=:]\s*)[^\s,;]+",
        r"(?i)(mysql(?:\+\w+)?://)[^\s]+",
        r"(?i)(dsn\s*[=:]\s*)[^\s,;]+",
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
