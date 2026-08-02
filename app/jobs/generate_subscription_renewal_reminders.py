"""Queue due subscription reminders without sending email directly."""

import argparse
import json
import logging

from app.services.schema_compatibility_service import validate_runtime_schema
from app.services.subscription_reminder_service import generate_subscription_renewal_reminders


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        validate_runtime_schema()
        summary = generate_subscription_renewal_reminders(dry_run=args.dry_run)
        print(json.dumps(summary, sort_keys=True))
        return 1 if summary["errors"] else 0
    except Exception:
        logging.getLogger(__name__).exception("Renewal reminder generation failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
