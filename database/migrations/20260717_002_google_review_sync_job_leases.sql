-- Phase 6C renewable ownership leases for Google review synchronization jobs.

ALTER TABLE google_review_sync_jobs
    ADD COLUMN worker_id VARCHAR(255) NULL AFTER active_business_id,
    ADD COLUMN lease_expires_at DATETIME(6) NULL AFTER worker_id,
    ADD COLUMN heartbeat_at DATETIME(6) NULL AFTER lease_expires_at,
    ADD KEY idx_google_review_sync_status_lease (status, lease_expires_at);
