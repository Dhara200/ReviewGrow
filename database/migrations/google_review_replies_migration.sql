USE reputation_db;

SET @schema_name = DATABASE();

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE businesses ADD COLUMN use_reviewer_name BOOLEAN DEFAULT TRUE', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'businesses' AND column_name = 'use_reviewer_name');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE businesses ADD COLUMN reply_tone VARCHAR(30) DEFAULT ''professional''', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'businesses' AND column_name = 'reply_tone');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE businesses ADD COLUMN max_reply_words INT DEFAULT 120', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'businesses' AND column_name = 'max_reply_words');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE businesses ADD COLUMN auto_generate_replies_for_new_reviews BOOLEAN DEFAULT TRUE', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'businesses' AND column_name = 'auto_generate_replies_for_new_reviews');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE businesses ADD COLUMN auto_post_replies BOOLEAN DEFAULT FALSE', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'businesses' AND column_name = 'auto_post_replies');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD COLUMN google_review_id VARCHAR(255)', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'reviews' AND column_name = 'google_review_id');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD COLUMN source_platform VARCHAR(50) NOT NULL DEFAULT ''google''', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'reviews' AND column_name = 'source_platform');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

UPDATE reviews
SET source_platform=source
WHERE source_platform='google'
AND source IS NOT NULL
AND source <> '';

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD COLUMN reply_status ENUM(''pending'',''approved'',''posted'',''failed'') DEFAULT ''pending''', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'reviews' AND column_name = 'reply_status');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD COLUMN reply_generated_at DATETIME', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'reviews' AND column_name = 'reply_generated_at');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD COLUMN reply_posted_at DATETIME', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'reviews' AND column_name = 'reply_posted_at');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD COLUMN reply_error_message TEXT', 'SELECT 1') FROM information_schema.columns WHERE table_schema = @schema_name AND table_name = 'reviews' AND column_name = 'reply_error_message');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0, 'ALTER TABLE reviews ADD INDEX idx_reviews_google_review_id (google_review_id)', 'SELECT 1') FROM information_schema.statistics WHERE table_schema = @schema_name AND table_name = 'reviews' AND index_name = 'idx_reviews_google_review_id');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS google_review_reply_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    review_id INT NOT NULL,
    business_id INT NOT NULL,
    user_id INT NOT NULL,
    google_review_id VARCHAR(255),
    reply_text TEXT NOT NULL,
    status ENUM('posted','failed') NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_reply_logs_review_id (review_id),
    INDEX idx_reply_logs_business_id (business_id),
    FOREIGN KEY (review_id)
        REFERENCES reviews(id)
        ON DELETE CASCADE,
    FOREIGN KEY (business_id)
        REFERENCES businesses(id)
        ON DELETE CASCADE,
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);
