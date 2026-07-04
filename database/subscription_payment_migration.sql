USE reputation_db;

CREATE TABLE IF NOT EXISTS subscriptions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    plan_name VARCHAR(50) NOT NULL,
    status ENUM('active','expired','cancelled','disabled') DEFAULT 'expired',
    subscription_start_date DATETIME NULL,
    subscription_end_date DATETIME NULL,
    review_credits INT DEFAULT 500,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_subscriptions_user_id (user_id),
    INDEX idx_subscriptions_status (status),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    subscription_id INT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(10) DEFAULT 'INR',
    payment_method VARCHAR(50) DEFAULT 'UPI',
    payment_status ENUM('pending','success','failed','rejected') DEFAULT 'pending',
    transaction_id VARCHAR(255) NOT NULL,
    payment_gateway VARCHAR(50) DEFAULT 'manual_upi',
    paid_at DATETIME NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_payments_user_id (user_id),
    INDEX idx_payments_subscription_id (subscription_id),
    INDEX idx_payments_payment_status (payment_status),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE,
    FOREIGN KEY (subscription_id)
        REFERENCES subscriptions(id)
        ON DELETE SET NULL
);
