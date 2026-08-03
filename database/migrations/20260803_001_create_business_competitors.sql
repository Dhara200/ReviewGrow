-- Persist verified GBP source-location metadata and manually selected competitors.
ALTER TABLE google_business_connections
    ADD COLUMN google_place_id VARCHAR(255) NULL AFTER google_location_name,
    ADD COLUMN latitude DECIMAL(10,7) NULL AFTER google_place_id,
    ADD COLUMN longitude DECIMAL(10,7) NULL AFTER latitude,
    ADD COLUMN primary_category VARCHAR(120) NULL AFTER longitude,
    ADD COLUMN formatted_address VARCHAR(500) NULL AFTER primary_category;

CREATE TABLE business_competitors (
    id INT AUTO_INCREMENT PRIMARY KEY,
    business_id INT NOT NULL,
    google_place_id VARCHAR(255) NOT NULL,
    competitor_name VARCHAR(255) NOT NULL,
    formatted_address VARCHAR(500) NULL,
    latitude DECIMAL(10,7) NULL,
    longitude DECIMAL(10,7) NULL,
    primary_type VARCHAR(120) NULL,
    google_maps_url VARCHAR(1000) NULL,
    rating DECIMAL(2,1) NULL,
    user_rating_count INT NULL,
    business_status VARCHAR(50) NULL,
    distance_meters INT NULL,
    last_refreshed_at DATETIME NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_business_competitor_place (business_id, google_place_id),
    INDEX idx_business_competitors_active (business_id, is_active),
    INDEX idx_business_competitors_refreshed (last_refreshed_at),
    CONSTRAINT fk_business_competitors_business
        FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE
);
