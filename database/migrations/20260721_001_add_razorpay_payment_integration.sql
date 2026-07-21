USE reputation_db;

ALTER TABLE payments
    MODIFY COLUMN payment_status ENUM(
        'pending','success','failed','rejected','created','attempted','paid','refunded','needs_review'
    ) NOT NULL DEFAULT 'pending',
    ADD COLUMN plan_code VARCHAR(50) NULL AFTER subscription_id,
    ADD COLUMN amount_paise BIGINT UNSIGNED NULL AFTER amount,
    ADD COLUMN razorpay_order_id VARCHAR(255) NULL AFTER payment_gateway,
    ADD COLUMN razorpay_payment_id VARCHAR(255) NULL AFTER razorpay_order_id,
    ADD COLUMN failure_code VARCHAR(100) NULL AFTER paid_at,
    ADD COLUMN failure_reason VARCHAR(255) NULL AFTER failure_code,
    ADD COLUMN processed_at DATETIME NULL AFTER failure_reason,
    ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP AFTER created_at,
    ADD UNIQUE KEY uniq_payments_razorpay_order_id (razorpay_order_id),
    ADD UNIQUE KEY uniq_payments_razorpay_payment_id (razorpay_payment_id),
    ADD INDEX idx_payments_user_status (user_id, payment_status),
    ADD INDEX idx_payments_created_at (created_at);

UPDATE payments
SET payment_gateway='manual_upi'
WHERE payment_gateway IS NULL OR payment_gateway='';

UPDATE payments
SET amount_paise=ROUND(amount * 100)
WHERE amount_paise IS NULL;
