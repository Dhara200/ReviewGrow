CREATE TABLE IF NOT EXISTS google_business_media_uploads (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    business_id INT NOT NULL,
    google_connection_id INT NULL,
    google_account_id VARCHAR(255),
    google_location_id VARCHAR(255),
    google_media_name VARCHAR(255),
    category VARCHAR(50) NOT NULL,
    original_filename VARCHAR(255),
    content_type VARCHAR(120),
    file_size_bytes INT DEFAULT 0,
    status ENUM('success','failed') NOT NULL,
    google_url TEXT,
    thumbnail_url TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_google_media_business (business_id, created_at),
    INDEX idx_google_media_user (user_id, created_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE,
    FOREIGN KEY (business_id)
        REFERENCES businesses(id)
        ON DELETE CASCADE,
    FOREIGN KEY (google_connection_id)
        REFERENCES google_business_connections(id)
        ON DELETE SET NULL
);
