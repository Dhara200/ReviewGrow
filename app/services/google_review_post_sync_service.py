from app.services.analysis_job_service import create_analysis_job
from app.services.business_analytics_service import refresh_business_review_analytics


def perform_google_review_post_sync(
    user_id,
    business_id,
    sync_result,
    google_location_id,
    on_analytics_error=None,
):
    """Apply the business-side effects that follow a successful Google sync.

    ``on_analytics_error`` exists solely to preserve the legacy route's
    historical best-effort analytics behavior. Workers omit it so any failed
    post-sync action fails the queue job.
    """
    analytics_result = None
    if sync_result["inserted_count"] or sync_result["updated_count"]:
        try:
            analytics_result = refresh_business_review_analytics(
                business_id,
                mark_consultant_outdated=True,
                source="google",
                google_location_id=google_location_id,
                require_google_review_id=True,
            )
        except Exception as error:
            if on_analytics_error is None:
                raise
            on_analytics_error(error)

    analysis_job_id, analysis_job_created = create_analysis_job(
        user_id,
        business_id,
        force_reanalysis=False,
    )
    return {
        "analytics_result": analytics_result,
        "analysis_job_id": analysis_job_id,
        "analysis_job_created": analysis_job_created,
    }
