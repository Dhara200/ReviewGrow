-- Harden the existing MySQL AI queue and support asynchronous consultant jobs.

ALTER TABLE analysis_jobs
    ADD COLUMN job_type ENUM('review_analysis','ai_consultant')
        NOT NULL DEFAULT 'review_analysis' AFTER business_id,
    ADD COLUMN operation_key VARCHAR(255) NULL AFTER job_type,
    ADD COLUMN active_operation_key VARCHAR(255) NULL AFTER operation_key,
    ADD COLUMN worker_id VARCHAR(255) NULL AFTER active_operation_key,
    ADD COLUMN lease_expires_at DATETIME(6) NULL AFTER worker_id,
    ADD COLUMN heartbeat_at DATETIME(6) NULL AFTER lease_expires_at,
    ADD COLUMN attempt_count INT UNSIGNED NOT NULL DEFAULT 0 AFTER heartbeat_at,
    ADD COLUMN max_attempts INT UNSIGNED NOT NULL DEFAULT 3 AFTER attempt_count,
    ADD COLUMN next_attempt_at DATETIME(6) NULL AFTER max_attempts,
    ADD COLUMN result_consultant_report_id INT NULL AFTER latest_report_id,
    ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP AFTER completed_at;

UPDATE analysis_jobs
SET operation_key=CONCAT('review_analysis:', business_id, ':historical:', id);

UPDATE analysis_jobs jobs
JOIN (
    SELECT business_id, MAX(id) AS canonical_job_id
    FROM analysis_jobs
    WHERE status IN ('pending','processing')
    GROUP BY business_id
) active_jobs ON active_jobs.business_id=jobs.business_id
SET jobs.active_operation_key=CASE
    WHEN jobs.id=active_jobs.canonical_job_id
        THEN CONCAT('review_analysis:', jobs.business_id)
    ELSE CONCAT('review_analysis:', jobs.business_id, ':historical-active:', jobs.id)
END
WHERE jobs.status IN ('pending','processing');

ALTER TABLE analysis_jobs
    MODIFY COLUMN operation_key VARCHAR(255) NOT NULL,
    ADD UNIQUE KEY uniq_analysis_jobs_active_operation (active_operation_key),
    ADD KEY idx_analysis_jobs_claim (status, next_attempt_at, created_at),
    ADD KEY idx_analysis_jobs_status_lease (status, lease_expires_at),
    ADD KEY idx_analysis_jobs_business_type_created
        (business_id, job_type, created_at),
    ADD CONSTRAINT fk_analysis_jobs_consultant_report
        FOREIGN KEY (result_consultant_report_id)
        REFERENCES ai_consultant_reports(id)
        ON DELETE SET NULL;
