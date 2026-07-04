USE reputation_db;

SET @schema_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE reviews ADD COLUMN google_location_id VARCHAR(255) NULL',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = @schema_name
    AND table_name = 'reviews'
    AND column_name = 'google_location_id'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX idx_reviews_business_google_location ON reviews(business_id, google_location_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = @schema_name
    AND table_name = 'reviews'
    AND index_name = 'idx_reviews_business_google_location'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
