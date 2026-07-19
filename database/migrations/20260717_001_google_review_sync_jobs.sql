-- Phase 1 infrastructure for future asynchronous Google review synchronization.
-- Parent key types are resolved from the active database at migration time so
-- foreign-key signedness and width exactly match production.

SET @schema_name = DATABASE();

SET @user_id_type = (
    SELECT COLUMN_TYPE
    FROM information_schema.columns
    WHERE table_schema = @schema_name
      AND table_name = 'users'
      AND column_name = 'id'
    LIMIT 1
);

SET @business_id_type = (
    SELECT COLUMN_TYPE
    FROM information_schema.columns
    WHERE table_schema = @schema_name
      AND table_name = 'businesses'
      AND column_name = 'id'
    LIMIT 1
);

-- CONCAT returns NULL if either required parent type is unavailable. PREPARE
-- then stops the migration instead of creating guessed or incompatible keys.
SET @create_google_review_sync_jobs = CONCAT(
    'CREATE TABLE google_review_sync_jobs (',
    'id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,',
    'user_id ', @user_id_type, ' NOT NULL,',
    'business_id ', @business_id_type, ' NOT NULL,',
    'status ENUM(''pending'',''processing'',''completed'',''failed'') NOT NULL DEFAULT ''pending'',',
    'fetched_count INT UNSIGNED NOT NULL DEFAULT 0,',
    'inserted_count INT UNSIGNED NOT NULL DEFAULT 0,',
    'updated_count INT UNSIGNED NOT NULL DEFAULT 0,',
    'error_message TEXT NULL,',
    'created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,',
    'started_at DATETIME NULL,',
    'completed_at DATETIME NULL,',
    'updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,',
    'active_business_id ', @business_id_type, ' NULL,',
    'PRIMARY KEY (id),',
    'UNIQUE KEY uniq_google_review_sync_active_business (active_business_id),',
    'KEY idx_google_review_sync_status_created (status, created_at),',
    'KEY idx_google_review_sync_user_created (user_id, created_at),',
    'KEY idx_google_review_sync_business_created (business_id, created_at),',
    'CONSTRAINT fk_google_review_sync_user FOREIGN KEY (user_id) ',
        'REFERENCES users(id) ON DELETE CASCADE,',
    'CONSTRAINT fk_google_review_sync_business FOREIGN KEY (business_id) ',
        'REFERENCES businesses(id) ON DELETE CASCADE',
    ') ENGINE=InnoDB'
);

PREPARE create_google_review_sync_jobs_stmt
    FROM @create_google_review_sync_jobs;
EXECUTE create_google_review_sync_jobs_stmt;
DEALLOCATE PREPARE create_google_review_sync_jobs_stmt;
