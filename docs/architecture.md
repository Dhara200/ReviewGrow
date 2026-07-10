# Application architecture

This project is a multi-layer Flask application aimed at helping businesses manage reviews, generate AI insights, and sync with Google Business Profile data.

## 1. Core architecture

The application is made of four major layers:

1. Web layer
   - Flask application in [app/app.py](app/app.py)
   - Blueprints in [app/routes](app/routes)
   - HTML templates in [app/templates](app/templates)

2. Business logic layer
   - Services in [app/services](app/services)
   - Handles review parsing, AI analysis, Google sync, subscriptions, and background jobs

3. Data layer
   - MySQL database
   - Connection handling in [app/services/database_service.py](app/services/database_service.py)
   - Schema initialization in [database/init.sql](database/init.sql)

4. Background processing layer
   - Worker process started by [worker.py](worker.py)
   - Used for long-running AI analysis jobs and other asynchronous work

## 2. Main runtime components

### Web app
The web app handles:
- user authentication
- business creation and management
- review uploads and display
- dashboard generation
- reports and subscription flows

### Worker
The worker process is responsible for:
- polling pending analysis jobs
- processing reviews in batches
- generating AI analysis results
- updating job state in MySQL

### MySQL database
The database stores:
- users and authentication state
- businesses
- reviews and analysis results
- analysis job status
- AI usage metrics
- Google connection metadata
- subscription and payment records

## 3. Request flow example

A typical review-analysis flow looks like this:
1. A user uploads reviews for a business.
2. The review route inserts or updates records in the database.
3. The app creates an analysis job row in the analysis_jobs table.
4. The worker detects the pending job.
5. The worker calls AI services and updates review records and job status.
6. The dashboard and report pages read the updated results.

This separation of web app and worker is important because AI processing can be slow and should not block the user interface.

## 4. External integrations

The app integrates with:
- Google APIs for Google Business Profile data and review synchronization
- Gemini AI services for review analysis and reply generation
- Optional payment/subscription flows for paid access

These integrations are configured through environment variables in [app/config.py](app/config.py).

## 5. Storage strategy

The current project uses:
- MySQL for structured application data
- uploads folder for local file storage during development
- optional cloud storage in production for better scaling

For ECS Fargate, it is better to use:
- EFS for persistent MySQL data
- S3 or EFS for uploaded files

This avoids relying on ephemeral container storage.

## 6. Why the worker exists

The worker exists because AI analysis is not ideal to run inline inside the web request thread.

If the web app processed everything synchronously:
- requests would become slow
- users would see timeouts
- the app would be harder to scale

Instead, the web app creates jobs, and the worker processes them asynchronously.

## 7. Deployment architecture for ECS Fargate

For production deployment, a good architecture is:
- Application Load Balancer in front of the Flask web app
- ECS Fargate service for the web app
- ECS Fargate service for the worker
- ECS Fargate service for MySQL
- EFS for MySQL persistence
- Secrets Manager for environment variables
- CloudWatch Logs for monitoring

This is the model described in [docs/ecs-fargate-mysql-guide.md](docs/ecs-fargate-mysql-guide.md).

## 8. Scaling considerations for many customers

The app is designed to support multiple businesses and users, but scaling becomes more important as the number of customers grows.

For 100s of customers, the following matters most:
- separate web and worker services
- enough CPU and memory for the worker
- persistent MySQL storage
- monitoring and logging
- eventually, a queue-based job system instead of polling

## 9. Directory map

Key folders:
- [app](app) contains the Flask app
- [app/routes](app/routes) contains route definitions
- [app/services](app/services) contains service logic
- [app/templates](app/templates) contains HTML templates
- [database](database) contains schema and migration SQL
- [docs](docs) contains documentation
- [uploads](uploads) contains sample upload files

## 10. Summary

In simple terms, the application works like this:
- the web app handles user interaction
- the worker handles heavy background jobs
- MySQL stores the app’s state
- Google and AI APIs add intelligence to reviews and replies

That structure makes the app easier to scale and operate than keeping everything inside one request path.
