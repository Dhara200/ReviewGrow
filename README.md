# ReviewGrow — AI-Powered Reputation Management SaaS

ReviewGrow is an AI-powered SaaS platform designed to help businesses manage their online reputation, analyze customer feedback, monitor Google Business Profile reviews, and generate intelligent, personalized responses using AI.

The platform provides business owners with a centralized dashboard to manage reviews, understand customer sentiment, identify recurring complaints and praise, and improve their online reputation through actionable insights.

## Key Features

### Google Business Profile Integration

* Connect Google Business Profiles through OAuth 2.0
* Sync and manage live customer reviews
* View reviews through a centralized dashboard
* Support for multiple businesses under a single user account
* Monitor review ratings and customer feedback

### AI-Powered Review Management

* AI-generated personalized review responses
* Context-aware responses based on customer feedback
* Business-owner approval workflow before publishing replies
* AI-powered review analysis using Google Gemini
* Sentiment analysis and customer feedback classification

### AI Business Insights

* Analyze live customer reviews
* Identify recurring customer complaints
* Detect frequently mentioned positive experiences
* Generate actionable business recommendations
* Track reputation and sentiment trends over time
* Convert customer feedback into practical improvement opportunities

### Review Analytics Dashboard

* Total review statistics
* Star-rating distribution
* Sentiment analysis
* Review filtering
* Review trends
* Business performance insights
* AI-generated summaries and recommendations

### Review Import and Analysis

* Upload reviews using CSV or Excel files
* Store and process historical review data
* Generate AI-powered reports
* Download generated reports as PDF

### Multi-Business SaaS Architecture

* Multiple businesses per user account
* Business-specific dashboards
* Separate review and analytics data for each business
* Subscription-based access control
* Administrative management capabilities

### Background Processing

ReviewGrow includes a dedicated background worker for asynchronous and AI-related workloads.

The application and worker run from the same immutable Docker image with separate runtime commands:

* Web application: Gunicorn
* Background worker: Python worker process

## Technology Stack

### Backend

* Python
* Flask
* Gunicorn
* MySQL 8.0

### AI

* Google Gemini API
* AI-powered sentiment analysis
* AI-generated review responses
* AI business recommendations and insights

### Google Integration

* Google OAuth 2.0
* Google Business Profile APIs

### Infrastructure and DevOps

* Docker
* Docker Compose
* AWS EC2
* Amazon Elastic Container Registry (ECR)
* GitHub Actions
* Apache reverse proxy
* Git commit SHA-based Docker image versioning
* Automated health checks
* Image-based rollback strategy

## High-Level Architecture

```text
User
  |
  v
Domain / HTTPS
  |
  v
Apache Reverse Proxy
  |
  v
Docker Compose on AWS EC2
  |
  +-------------------------------+
  |                               |
  v                               v
Flask App                     Background Worker
Gunicorn                      Python Worker
  |                               |
  +---------------+---------------+
                  |
                  v
               MySQL
                  |
                  +--------------------+
                  |                    |
                  v                    v
        Google Business Profile   Google Gemini API
```

## Container Architecture

The production environment runs three primary containers:

```text
reputation_app
    |
    |-- Gunicorn
    |-- Flask application
    |-- Port 5000
    |
    +------------------+
                       |
                       v
                reputation_mysql
                    MySQL 8.0
                       ^
                       |
    +------------------+
    |
reputation_worker
    |
    |-- Background processing
    |-- AI-related workloads
```

Both the application and background worker use the same versioned Docker image.

The worker overrides the default container command to run:

```text
python worker.py
```

### Database migrations before startup

The Flask application and background worker never create or repair database
schema at runtime. Apply all versioned migrations explicitly before starting or
restarting either process:

```bash
./scripts/run_migrations.sh
```

The runtime database account only needs application data and read-only metadata
permissions; schema-changing privileges belong to the deployment migration
process. Startup fails safely when the database is unavailable or required
migration-managed columns are missing.

The web application runs through Gunicorn.

## CI/CD Pipeline

ReviewGrow uses an automated GitHub Actions CI/CD pipeline.

Every push to the `main` branch triggers the production deployment workflow.

```text
Developer Pushes to main
          |
          v
GitHub Actions
          |
          v
Build Docker Image
          |
          v
Tag Image with Git Commit SHA
          |
          v
Push Image to Amazon ECR
          |
          v
Copy Production Compose Configuration
          |
          v
Authenticate EC2 with Amazon ECR
          |
          v
Pull Exact SHA-Tagged Image
          |
          v
Recreate Application Containers
          |
          v
Application Health Check
          |
          v
Verify Running Image SHA
          |
          v
Deployment Successful
```

### Immutable Image Versioning

Every production image is tagged using its Git commit SHA.

Example:

```text
reviewgrow-app-app:<git-commit-sha>
```

This provides:

* Traceable deployments
* Reproducible releases
* Clear mapping between source code and production containers
* Reliable rollback to previous application versions

## Deployment Verification

The deployment pipeline automatically verifies:

* Application health endpoint
* Successful container startup
* Exact ECR image running in the application container
* Exact ECR image running in the worker container
* SHA-tag consistency between the expected and deployed version

A deployment is marked successful only after the health and image verification checks pass.

## Rollback Strategy

Previous Docker images remain versioned using Git commit SHA tags.

A previous application version can be restored by redeploying the required SHA-tagged image.

```text
Current Version
      |
      | Production issue detected
      v
Select Previous Working SHA
      |
      v
Pull Previous Image from ECR
      |
      v
Recreate App and Worker Containers
      |
      v
Run Health Check
      |
      v
Verify Running Image
      |
      v
Rollback Complete
```

This allows application rollback without rebuilding the source code.

> Database schema changes require separate migration and rollback planning.

## Production Container Setup

The production deployment consists of:

* `reputation_app` — Flask application served using Gunicorn
* `reputation_worker` — Background processing worker
* `reputation_mysql` — MySQL 8.0 database

The application and worker communicate with MySQL through the internal Docker network.

MySQL is not exposed publicly in the production Compose configuration.

## Security Practices

The project follows several production security practices:

* Environment-based secret management
* `.env` files excluded from version control
* Secrets not stored directly in Docker images
* Non-public MySQL container networking
* OAuth 2.0 for Google integrations
* Login rate limiting and temporary account lockout
* Production workloads deployed using immutable SHA-tagged images
* Health checks before deployment completion
* Separation of application configuration and source code


## Production Deployment

Production deployments are automatically handled through GitHub Actions when changes are pushed to the configured production branch.

The deployment process:

1. Checks out the source code.
2. Authenticates with AWS.
3. Builds the production Docker image.
4. Tags the image with the Git commit SHA.
5. Pushes the image to Amazon ECR.
6. Connects to the production EC2 instance.
7. Pulls the exact SHA-tagged image.
8. Recreates the application and worker containers.
9. Performs an application health check.
10. Verifies that the expected image version is running.

## Legal and OAuth pages

ReviewGrow provides the following public legal and account-data pages:

* `/privacy-policy`
* `/terms-of-service`
* `/data-deletion`

For production, enter the corresponding `https://reviewgrow.in` URLs in the Google Auth Platform branding and OAuth consent configuration where applicable. Keep the configured homepage, privacy-policy, terms-of-service, and data-deletion URLs aligned with the publicly verified domain. These routes do not change the application's OAuth scopes.

Google Cloud Console configuration is an external deployment step and is not performed by the application or its deployment workflow.

## SEO

ReviewGrow exposes public search-engine discovery files at:

* Sitemap: `https://reviewgrow.in/sitemap.xml`
* Robots: `https://reviewgrow.in/robots.txt`
* Google Search Console submission value: `sitemap.xml`

Only public, indexable pages should be included in the sitemap. Authenticated,
administrative, API, OAuth, and user-specific routes must remain excluded.

## Project Status

ReviewGrow is under active development.

Current focus areas include:

* AI-powered review management
* Google Business Profile automation
* Business reputation analytics
* Customer feedback intelligence
* Scalable SaaS infrastructure
* Production monitoring and observability
* Improved deployment reliability

## Future Enhancements

Planned improvements include:

* Advanced AI-powered business recommendations
* Competitor reputation analysis
* WhatsApp-based review request workflows
* Review notification automation
* Advanced usage analytics
* Enhanced subscription and billing workflows

## Razorpay payments

Razorpay Standard Checkout setup, environment placeholders, migration steps, webhook configuration, go-live checks, troubleshooting, and rollback guidance are documented in [docs/razorpay-integration.md](docs/razorpay-integration.md). This integration provides one-time subscription purchases; automatic recurring Razorpay Subscriptions are not included.
* Expanded observability and monitoring
* Infrastructure scaling as customer usage grows

## Author

**Dhara Prasath**

DevOps Engineer | SaaS Builder

Built as an end-to-end SaaS project combining software development, artificial intelligence, cloud infrastructure, containerization, CI/CD, and production operations.

---

**ReviewGrow — Turn customer feedback into actionable business growth.**
