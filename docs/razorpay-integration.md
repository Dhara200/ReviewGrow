# Razorpay Standard Checkout

ReviewGrow uses Razorpay Orders and Standard Checkout for one-time, 30-day Starter plan purchases. It does not use Razorpay Subscriptions, mandates, or automatic renewal.

## Flow and configuration

The browser sends only `starter_monthly`. The backend resolves the authoritative INR price, creates a local transaction and Razorpay order, and returns the public key and order details. Checkout results are verified server-side against the owned local order, the Razorpay signature, amount, currency, and captured/paid provider state. A locked `processed_at` marker makes browser and webhook processing idempotent before the existing subscription entitlement is activated or extended.

Set these deployment variables (placeholders only):

```dotenv
RAZORPAY_KEY_ID=rzp_test_replace_me
RAZORPAY_KEY_SECRET=replace_me
RAZORPAY_WEBHOOK_SECRET=replace_with_random_webhook_secret
```

The app can start without a webhook secret. `/webhooks/razorpay` fails closed with HTTP 503 until it is configured. Secrets are never sent to templates or JSON; only `RAZORPAY_KEY_ID` is public.

## Test Mode and local setup

Use Razorpay Test Mode keys, install dependencies, migrate, rebuild, and run tests:

```bash
pip install -r requirements.txt
./scripts/run_migrations.sh
docker build -f docker/app/Dockerfile -t reviewgrow-app:local .
# For the checked-in deployment compose file, set ECR_REGISTRY and IMAGE_TAG
# to the rebuilt/published image coordinates, then:
docker compose up -d --force-recreate app worker
python -m unittest discover -s tests -p "test_*.py"
```

Test Checkout with Razorpay's documented test payment details. Confirm success activates/extends once, failure does not activate, replayed verification is harmless, and the admin transaction list shows the provider status on desktop and mobile.

## Dashboard webhook setup

After deployment:

1. Generate a strong secret, for example `openssl rand -hex 32`.
2. Store it as `RAZORPAY_WEBHOOK_SECRET` in the deployment environment.
3. In Razorpay Dashboard Test Mode, configure `https://reviewgrow.in/webhooks/razorpay`.
4. Select `order.paid`, `payment.captured`, and `payment.failed`.
5. Enter the same secret in Razorpay Dashboard.
6. Restart/redeploy, then send Test Mode events.

The endpoint validates `X-Razorpay-Signature` over the raw request bytes. Duplicate `order.paid` and `payment.captured` events return 2xx without extending access twice.

## Go-live checklist

- Apply `database/migrations/20260721_001_add_razorpay_payment_integration.sql` and back up the database first.
- Complete Test Mode success, failure, dismissal, replay, and webhook tests.
- Confirm the public HTTPS webhook is reachable and its deliveries succeed.
- Replace test key ID/secret with live credentials through the secret manager; never commit them.
- Create a separate strong live webhook secret and configure the same three live events.
- Check amount, business identity, settlement settings, policies, logs, and admin reconciliation.

## Troubleshooting and rollback

- `Payment service is not configured`: key ID or key secret is absent.
- Webhook 503: add `RAZORPAY_WEBHOOK_SECRET` and restart.
- Webhook 401: confirm the dashboard and environment secrets match and no proxy changes the body.
- Verification 409: payment is not yet captured/paid; allow webhook reconciliation.
- Unknown order: the event/order was not created by this ReviewGrow environment.

For application rollback, deploy the previous application version and disable the Razorpay Dashboard webhook. Keep the additive payment columns and all rows; do not reverse the migration while Razorpay transactions exist. Historical `manual_upi` records and their approval audit data remain readable. A schema rollback, if ever required after a verified backup and data export, must be a new forward migration rather than editing the applied migration.
