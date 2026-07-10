# Database design overview

This application uses MySQL as its primary relational database. The database layer is managed through [app/services/database_service.py](app/services/database_service.py), which opens connections and ensures the schema exists when the app starts.

## 1. Database access model

The app connects to MySQL using the mysql-connector Python package. Configuration comes from environment variables in [app/config.py](app/config.py):
- DB_HOST
- DB_PORT
- DB_NAME
- DB_USER
- DB_PASSWORD

The app also runs schema initialization logic on startup through ensure_mvp_schema(). This means the app can create missing tables and add missing columns automatically.

## 2. Initial schema source

The base schema is defined in [database/init.sql](database/init.sql). Additional schema changes are also handled dynamically in [app/services/database_service.py](app/services/database_service.py).

## 3. Core tables

### users
Stores account information for each user.
Key fields:
- id
- name
- email
- password_hash
- role
- created_at
- updated_at

This table is the anchor for most user-specific records.

### businesses
Stores each business created by a user.
Key fields:
- id
- user_id
- business_name
- business_type
- city/state/country
- reply settings and automation flags

Each business belongs to one user and can have many reviews and jobs.

### reviews
Stores review data imported from CSV/Excel or synced from Google.
Key fields:
- id
- business_id
- source
- rating
- review_text
- reviewer_name
- review_date
- analysis_status
- sentiment
- summary
- ai_reply
- suggested_reply
- reply_status
- analyzed_at

This is one of the most important tables because it holds the review content and AI-generated analysis.

### analysis_jobs
Tracks background tasks for review analysis.
Key fields:
- id
- user_id
- business_id
- status
- total_reviews
- processed_reviews
- failed_reviews
- error_message
- created_at
- started_at
- completed_at

The web app creates a row here, and the worker updates it as processing progresses.

### ai_usage_logs
Stores AI request usage and cost telemetry.
Key fields:
- user_id
- business_id
- provider
- model_name
- operation_type
- input_tokens
- output_tokens
- total_tokens
- estimated_cost
- request_status
- response_time_ms

This table helps track usage and support billing or quota logic.

### ai_monthly_usage
Aggregates AI usage by month for reporting and limits.
Key fields:
- user_id
- business_id
- usage_month
- total_requests
- total_tokens
- total_estimated_cost

### google_business_connections
Stores Google Business Profile connection data.
Key fields:
- user_id
- business_id
- google_account_id
- google_location_id
- access_token
- refresh_token
- token_expiry
- connection_status

This table connects your app users to external Google business accounts.

### google_review_reply_logs
Stores audit information for generated or posted Google replies.
Key fields:
- review_id
- business_id
- user_id
- google_review_id
- reply_text
- status
- error_message

### subscriptions and payments
These tables support the paid plan flow:
- subscriptions stores plan status and review credits
- payments tracks payment success or failure

## 4. Relationships

The main relationships are:
- one user has many businesses
- one business has many reviews
- one business has many analysis jobs
- one review belongs to one business
- one Google connection belongs to one business and one user
- one subscription belongs to one user
- one payment belongs to one user and may reference a subscription

## 5. Why schema updates are handled automatically

The app uses ensure_mvp_schema() to make schema evolution easier.

This helps because:
- local development databases may be missing new columns
- the app can add missing tables and columns automatically
- deployment becomes less fragile when the schema changes over time

## 6. Important operational notes

### Persistence in ECS
If you run this app on ECS Fargate, MySQL should not rely on container-local storage. Use:
- EFS for MySQL data persistence
- secure environment variables for credentials

### Backup strategy
Because the app stores business and review data in MySQL, regular backups are essential.

### Scaling note
For a large number of customers, database performance matters. You may eventually need:
- better indexing
- query tuning
- connection pooling
- moving to managed MySQL like RDS or Aurora

## 7. Practical summary

If you want to understand the database mentally, think of it as:
- users = who is using the system
- businesses = what they manage
- reviews = the feedback being analyzed
- analysis_jobs = the background work queue
- ai_usage_logs = AI consumption and cost
- google_business_connections = external Google connectivity
- subscriptions/payments = commercial access

That structure is what allows the app to support review import, AI analysis, Google sync, dashboards, and billing in one system.
