# API and route overview

This application is a Flask web app with server-rendered HTML pages and a set of backend routes for business management, review processing, AI analysis, Google integration, subscriptions, and admin operations.

## 1. How the app is structured

The app uses Flask blueprints to organize routes:
- auth routes for login, registration, logout, and account deletion
- business routes for creating and managing businesses
- review routes for uploading and reviewing customer feedback
- analysis routes for triggering and tracking AI analysis jobs
- dashboard routes for reports and analytics
- Google business routes for Google Business Profile syncing and reply workflows
- subscription routes for pricing and payment handling
- admin routes for internal management

The main Flask app is created in [app/app.py](app/app.py), and the route modules live in [app/routes](app/routes).

## 2. Main request flow

A typical user flow is:
1. User signs up or logs in.
2. User creates or selects a business.
3. Reviews are uploaded or synced from Google.
4. An analysis job is created.
5. The worker processes the job and updates review analysis results.
6. The user can view reports, replies, and dashboard insights.

## 3. Authentication and account routes

These routes handle account access and user session management:
- GET/POST /register-page
- GET/POST /login-page
- GET /logout
- DELETE /account/delete

These routes rely on Flask session state and the users table in the database.

## 4. Business management routes

Core business routes include:
- GET /create-business
- GET /my-businesses
- POST /business/delete/<business_id>
- GET /upload-reviews/<business_id>

These routes allow a user to manage their businesses and start review import workflows.

## 5. Review handling routes

Review routes cover upload, history, analysis, and deletion:
- POST /reviews/upload-ui
- GET /reviews/history/<business_id>
- POST /reviews/analyze/<review_id>
- GET /reviews/analysis/<review_id>
- POST /reviews/delete/<review_id>

These routes are used when a user imports review data from CSV/Excel or from a connected Google account.

## 6. Analysis job routes

The app supports asynchronous analysis jobs for batches of reviews:
- POST /businesses/<business_id>/analysis-jobs
- GET /analysis-jobs/<job_id>/status
- POST /analysis-jobs/<job_id>/retry

This is important because review analysis can be long-running. The web app creates a job record, and the worker picks it up and updates its status.

## 7. Google Business integration routes

Google-related routes handle OAuth and review synchronization:
- GET /businesses/<business_id>/google/connect
- GET /auth/google/start/<business_id>
- GET /auth/google/callback
- GET /businesses/<business_id>/google/select-location
- POST /businesses/<business_id>/google/select-location
- POST /businesses/<business_id>/google/disconnect
- GET /businesses/<business_id>/live-dashboard
- POST /businesses/<business_id>/google/sync-reviews
- GET/POST /businesses/<business_id>/photos
- POST /reviews/<review_id>/reply/regenerate
- POST /reviews/<review_id>/reply/approve
- POST /reviews/<review_id>/reply/post
- POST /businesses/<business_id>/google/sync-performance

These endpoints allow the app to connect to Google Business Profile data, sync reviews, manage replies, and show live performance metrics.

## 8. Subscription and pricing routes

Subscription-related routes include:
- GET /pricing
- POST /pricing/submit-payment

These routes are used for plan and payment management.

## 9. Dashboard and reporting routes

Reporting routes include:
- GET /report/<business_id>
- GET /report/<business_id>/pdf
- GET /dashboard/<business_id>

These pages usually summarize review performance, sentiment trends, and business insights.

## 10. Admin routes

Admin routes provide internal access for managing users, payments, and AI analysis data:
- GET /admin/dashboard
- GET /admin/ai-analysis
- GET /admin/users
- GET /admin/users/<user_id>
- POST /admin/users/<user_id>/delete
- GET /admin/payments
- POST /admin/payments/<payment_id>/approve
- POST /admin/payments/<payment_id>/reject

## 11. Health and deployment endpoints

The app exposes a health endpoint for monitoring and container health checks:
- GET /health

This endpoint confirms that the app can connect to MySQL. For ECS Fargate, a lightweight health path such as /healthz is also recommended.

## 12. Notes about the API style

This project is not a pure JSON API. Most interactions are HTML form submissions and server-rendered pages.

That means:
- The app is mostly browser-driven
- Some routes return templates instead of JSON
- Some actions are triggered via POST forms
- Background processing is handled by the worker process rather than the web request thread

## 13. Important implementation detail

The app uses environment variables from [app/config.py](app/config.py) to build database and external service connections. The database layer is centralized in [app/services/database_service.py](app/services/database_service.py), which opens connections and applies schema updates automatically when the app starts.
