USE reputation_db;

CREATE TABLE IF NOT EXISTS google_business_connections (
    id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL,
    business_id INT NOT NULL,

    google_account_id VARCHAR(255),
    google_account_email VARCHAR(255),
    google_location_id VARCHAR(255),
    google_location_name VARCHAR(255),

    access_token TEXT,
    refresh_token TEXT,
    token_expiry DATETIME,
    scope TEXT,
    scopes TEXT,
    connection_status VARCHAR(50) DEFAULT 'connected',

    is_connected BOOLEAN DEFAULT FALSE,
    last_sync_at DATETIME,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY unique_business_connection (user_id, business_id),
    UNIQUE KEY uniq_google_business_business (business_id),

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE,

    FOREIGN KEY (business_id)
        REFERENCES businesses(id)
        ON DELETE CASCADE
);

SET @schema_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE google_business_connections ADD COLUMN google_account_email VARCHAR(255)',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'google_business_connections'
    AND column_name = 'google_account_email'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE google_business_connections ADD COLUMN scopes TEXT',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'google_business_connections'
    AND column_name = 'scopes'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE google_business_connections ADD COLUMN connection_status VARCHAR(50) DEFAULT ''connected''',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'google_business_connections'
    AND column_name = 'connection_status'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE google_business_connections ADD UNIQUE KEY unique_business_connection (user_id, business_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = @schema_name
    AND table_name = 'google_business_connections'
    AND index_name = 'unique_business_connection'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE reviews ADD COLUMN external_review_id VARCHAR(255)',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'reviews'
    AND column_name = 'external_review_id'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE reviews ADD COLUMN review_rating DECIMAL(2,1)',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'reviews'
    AND column_name = 'review_rating'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE reviews ADD COLUMN review_created_at DATETIME',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'reviews'
    AND column_name = 'review_created_at'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE reviews ADD COLUMN review_updated_at DATETIME',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'reviews'
    AND column_name = 'review_updated_at'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
