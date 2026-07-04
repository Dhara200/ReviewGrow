from app.config import Config
from app.services.analysis_job_service import run_worker_forever
from app.services.database_service import ensure_mvp_schema


if __name__ == "__main__":
    try:
        ensure_mvp_schema()
    except Exception as error:
        print(f"Schema check skipped: {error}", flush=True)

    print("AI analysis worker started.", flush=True)
    run_worker_forever(
        poll_seconds=Config.AI_WORKER_POLL_SECONDS,
        batch_size=Config.AI_BATCH_SIZE
    )
