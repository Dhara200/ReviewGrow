-- Query-pattern indexes for tenant-scoped review, reporting, queue, and billing access.

CREATE INDEX idx_reviews_business_analysis_id
    ON reviews (business_id, analysis_status, id);

CREATE INDEX idx_reviews_business_analyzed_at
    ON reviews (business_id, analyzed_at);

CREATE INDEX idx_reviews_business_google_review
    ON reviews (business_id, google_review_id);

CREATE INDEX idx_reviews_business_location_external
    ON reviews (business_id, google_location_id, external_review_id);

CREATE INDEX idx_reports_business_generated
    ON reports (business_id, generated_at);

CREATE INDEX idx_analysis_jobs_status_created
    ON analysis_jobs (status, created_at);

CREATE INDEX idx_analysis_jobs_business_status_created
    ON analysis_jobs (business_id, status, created_at);

CREATE INDEX idx_google_review_sync_business_status_created
    ON google_review_sync_jobs (business_id, status, created_at);

CREATE INDEX idx_businesses_user_created
    ON businesses (user_id, created_at);

CREATE INDEX idx_subscriptions_user_created_id
    ON subscriptions (user_id, created_at, id);

CREATE INDEX idx_payments_user_created_id
    ON payments (user_id, created_at, id);

CREATE INDEX idx_payments_user_status_created
    ON payments (user_id, payment_status, created_at);
