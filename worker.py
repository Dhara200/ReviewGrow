import time
from uuid import uuid4

from app.app import app
from app.config import Config
from app.services.analysis_job_service import (
    claim_next_job,
    process_analysis_job,
    reset_stale_processing_jobs,
)
from app.services.google_review_sync_job_service import (
    claim_next_google_review_sync_job,
    process_google_review_sync_job,
    reset_stale_google_review_sync_jobs,
)


if __name__ == "__main__":
    worker_id = uuid4().hex
    print(f"Background worker started: {worker_id}", flush=True)
    with app.app_context():
        while True:
            reset_stale_google_review_sync_jobs()
            reset_stale_processing_jobs()

            sync_job = claim_next_google_review_sync_job(worker_id)
            if sync_job:
                process_google_review_sync_job(sync_job)
                continue

            analysis_job = claim_next_job()
            if analysis_job:
                process_analysis_job(analysis_job["id"], batch_size=Config.AI_BATCH_SIZE)
                continue

            time.sleep(Config.AI_WORKER_POLL_SECONDS)
