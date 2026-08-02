-- Read-optimized indexes for the admin email analytics dashboard.
ALTER TABLE email_jobs
    ADD KEY idx_email_jobs_created_status_type (created_at, status, email_type),
    ADD KEY idx_email_jobs_type_status_created (email_type, status, created_at),
    ADD KEY idx_email_jobs_sent_at (sent_at),
    ADD KEY idx_email_jobs_ses_message_id (ses_message_id);
